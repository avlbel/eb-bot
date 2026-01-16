from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from telegram.error import TelegramError

from config import get_settings
from db import (
    count_posts_for_date,
    get_due_polls,
    mark_poll_posted,
    mark_poll_error,
    mark_poll_skipped,
    pick_random_post,
    utc_now,
)
from timeweb_ai import TimewebAIError, generate_poll_options

logger = logging.getLogger(__name__)


async def poller_loop(state) -> None:
    """
    Фоновый планировщик ежедневных опросов.
    """
    logger.info("Poller loop initialized")
    while True:
        try:
            await run_poll_once(state)
        except Exception:
            logger.exception("Poller loop error")
        await asyncio.sleep(60)


async def run_poll_once(state, force: bool = False) -> dict[str, object]:
    settings = get_settings()
    pool = getattr(state, "db_pool", None)
    if pool is None or not settings.daily_poll_enabled:
        if settings.daily_poll_enabled and pool is None:
            logger.warning("Daily poll enabled but DB pool is not available")
        return {"ok": False, "reason": "db_not_available_or_poll_disabled"}

    poll_channels = settings.daily_poll_channel_ids
    if not poll_channels:
        logger.warning("Daily poll enabled but DAILY_POLL_CHANNEL_IDS is empty")
        return {"ok": False, "reason": "poll_channels_empty"}

    app = getattr(state, "telegram_app", None)
    if app is None:
        return {"ok": False, "reason": "telegram_app_not_ready"}

    now_utc = utc_now()
    due = await get_due_polls(pool, now_utc)
    if not due:
        return {"ok": False, "reason": "no_due_polls"}

    tz = ZoneInfo(settings.daily_poll_timezone)
    start_t = dtime(hour=settings.daily_poll_start_hour, minute=0)
    end_t = dtime(hour=settings.daily_poll_end_hour, minute=0)

    for row in due:
        channel_id = int(row["channel_id"])
        poll_date = row["poll_date"]

        if channel_id not in poll_channels:
            continue

        # окно публикации
        start_dt = datetime.combine(poll_date, start_t, tzinfo=tz)
        end_dt = datetime.combine(poll_date, end_t, tzinfo=tz)
        now_local = datetime.now(tz)
        if now_local > end_dt and not force:
            # Если окно уже прошло — помечаем как пропущенный опрос.
            await mark_poll_skipped(pool, channel_id, poll_date)
            logger.info(
                "Daily poll skipped: window passed (channel_id=%s, date=%s)",
                channel_id,
                poll_date,
            )
            continue

        # Нужно минимум N постов за день
        posts_count = await count_posts_for_date(pool, channel_id, poll_date)
        if posts_count < settings.daily_poll_min_posts and not force:
            logger.info(
                "Daily poll not posted: not enough posts (%s/%s) for channel_id=%s date=%s",
                posts_count,
                settings.daily_poll_min_posts,
                channel_id,
                poll_date,
            )
            continue

        post = await pick_random_post(pool, channel_id, poll_date)
        if not post:
            logger.info(
                "Daily poll not posted: no posts found for channel_id=%s date=%s",
                channel_id,
                poll_date,
            )
            continue

        photo_file_id = post.get("photo_file_id")
        if not photo_file_id:
            logger.info(
                "Daily poll not posted: chosen post has no photo_file_id (channel_id=%s date=%s)",
                channel_id,
                poll_date,
            )
            continue

        # Загружаем картинку
        try:
            tg_file = await app.bot.get_file(photo_file_id)
            image_bytes = bytes(await tg_file.download_as_bytearray())
        except TelegramError:
            logger.exception("Не удалось скачать картинку для опроса")
            return {"ok": False, "reason": "download_photo_failed", "channel_id": channel_id, "date": str(poll_date)}

        # Вопрос — фиксированный список
        question = random.choice(settings.daily_poll_questions)

        try:
            options = await generate_poll_options(
                image_bytes=image_bytes,
                question=question,
                options_count=settings.daily_poll_options_count,
            )
        except TimewebAIError as e:
            logger.exception("Не удалось сгенерировать варианты опроса через AI (2 попытки)")
            await mark_poll_error(pool, channel_id, poll_date, str(e))
            await mark_poll_skipped(pool, channel_id, poll_date)
            return {"ok": False, "reason": "ai_options_failed", "channel_id": channel_id, "date": str(poll_date)}

        try:
            poll_msg = await app.bot.send_poll(
                chat_id=channel_id,
                question=question,
                options=options,
                is_anonymous=True,
                allows_multiple_answers=False,
                open_period=settings.daily_poll_open_seconds,
                reply_to_message_id=int(post["message_id"]),
            )
        except TelegramError:
            logger.exception("Не удалось отправить опрос в канал")
            return {"ok": False, "reason": "send_poll_failed", "channel_id": channel_id, "date": str(poll_date)}

        await mark_poll_posted(
            pool=pool,
            channel_id=channel_id,
            poll_date=poll_date,
            poll_message_id=poll_msg.message_id,
            chosen_post_message_id=int(post["message_id"]),
            question=question,
            options=options,
        )
        return {"ok": True, "channel_id": channel_id, "date": str(poll_date), "poll_message_id": poll_msg.message_id}

    return {"ok": False, "reason": "no_poll_posted"}

