from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from config import get_settings
from timeweb_ai import TimewebAIError, generate_funny_caption, generate_poll_options

from db import (
    ensure_daily_poll,
    maybe_cleanup_old_posts,
    record_post,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class DiscussionRef:
    discussion_chat_id: int
    discussion_message_id: int
    ts: float


# key: (channel_chat_id, channel_message_id) -> DiscussionRef
_DISCUSSION_MAP: dict[tuple[int, int], DiscussionRef] = {}
_DISCUSSION_TTL_S = 60 * 60  # 1 час достаточно


# Дедупликация: не отвечать на каждое фото в альбоме (media_group), а только один раз на пост.
_PROCESSED_MEDIA_GROUPS: dict[str, float] = {}
_MEDIA_GROUP_TTL_S = 6 * 60 * 60  # 6 часов

# Дедупликация сообщений канала на случай повторной доставки апдейта
_PROCESSED_CHANNEL_MESSAGES: dict[tuple[int, int], float] = {}
_CHANNEL_MSG_TTL_S = 6 * 60 * 60


def _is_recent(ts: float, ttl_s: float) -> bool:
    return (time.time() - ts) <= ttl_s


def _mark_processed_channel_message(chat_id: int, message_id: int) -> bool:
    """
    Возвращает True если сообщение уже обрабатывали (и его надо пропустить),
    иначе помечает как обработанное и возвращает False.
    """
    key = (chat_id, message_id)
    ts = _PROCESSED_CHANNEL_MESSAGES.get(key)
    if ts is not None and _is_recent(ts, _CHANNEL_MSG_TTL_S):
        return True
    _PROCESSED_CHANNEL_MESSAGES[key] = time.time()
    return False


def _should_skip_media_group(media_group_id: str) -> bool:
    ts = _PROCESSED_MEDIA_GROUPS.get(media_group_id)
    if ts is not None and _is_recent(ts, _MEDIA_GROUP_TTL_S):
        return True
    _PROCESSED_MEDIA_GROUPS[media_group_id] = time.time()
    return False


async def _record_post_for_poll_if_needed(
    context: ContextTypes.DEFAULT_TYPE,
    channel_id: int,
    message_id: int,
    post_date,
    photo_file_id: str | None,
) -> None:
    settings = get_settings()
    pool = getattr(getattr(context, "application", None), "bot_data", {}).get("db_pool")
    if pool is None:
        return

    # Записываем посты для статистики всегда, если есть БД
    await maybe_cleanup_old_posts(pool, post_date, days=30)
    await record_post(pool, channel_id, message_id, post_date, photo_file_id)

    # Планируем опрос только для строго заданных каналов и при включённом режиме
    if not settings.daily_poll_enabled:
        return
    poll_channels = settings.daily_poll_channel_ids
    if not poll_channels or channel_id not in poll_channels:
        return

    # Планируем poll на сегодня, если ещё не создан
    await _ensure_poll_scheduled(pool, channel_id, post_date)


async def _ensure_poll_scheduled(pool, channel_id: int, poll_date) -> None:
    settings = get_settings()
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo
    import random

    tz = ZoneInfo(settings.daily_poll_timezone)
    start = dtime(hour=settings.daily_poll_start_hour, minute=0)
    end = dtime(hour=settings.daily_poll_end_hour, minute=0)

    # случайный момент в диапазоне
    start_dt = datetime.combine(poll_date, start, tzinfo=tz)
    end_dt = datetime.combine(poll_date, end, tzinfo=tz)
    if end_dt <= start_dt:
        end_dt = start_dt

    delta = (end_dt - start_dt).total_seconds()
    offset = random.uniform(0, max(delta, 0))
    scheduled_local = start_dt + timedelta(seconds=offset)
    scheduled_utc = scheduled_local.astimezone(timezone.utc)

    await ensure_daily_poll(pool, channel_id, poll_date, scheduled_utc)


def _discussion_map_put(channel_chat_id: int, channel_message_id: int, discussion_chat_id: int, discussion_message_id: int) -> None:
    _DISCUSSION_MAP[(channel_chat_id, channel_message_id)] = DiscussionRef(
        discussion_chat_id=discussion_chat_id,
        discussion_message_id=discussion_message_id,
        ts=time.time(),
    )


def _discussion_map_get(channel_chat_id: int, channel_message_id: int) -> DiscussionRef | None:
    ref = _DISCUSSION_MAP.get((channel_chat_id, channel_message_id))
    if ref is None:
        return None
    if time.time() - ref.ts > _DISCUSSION_TTL_S:
        _DISCUSSION_MAP.pop((channel_chat_id, channel_message_id), None)
        return None
    return ref


def _extract_origin_channel_and_msg_id(message) -> tuple[int, int] | None:
    """
    Пытаемся достать (channel_chat_id, channel_message_id) из автофорварда в linked-чате.
    Поддерживаем разные версии Bot API / PTB: forward_from_chat + forward_from_message_id или forward_origin.
    """
    # legacy fields
    fchat = getattr(message, "forward_from_chat", None)
    fmid = getattr(message, "forward_from_message_id", None)
    if fchat is not None and isinstance(getattr(fchat, "id", None), int) and isinstance(fmid, int):
        return int(fchat.id), int(fmid)

    # newer: forward_origin (MessageOriginChannel)
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        ochat = getattr(origin, "chat", None)
        omid = getattr(origin, "message_id", None)
        if ochat is not None and isinstance(getattr(ochat, "id", None), int) and isinstance(omid, int):
            return int(ochat.id), int(omid)
    return None


async def handle_discussion_auto_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Когда включены комментарии, в linked discussion group появляется автофорвард поста канала.
    Мы ловим его и сохраняем mapping: (channel_id, channel_msg_id) -> (discussion_chat_id, discussion_msg_id).
    """
    msg = update.effective_message
    if msg is None or msg.chat is None:
        return

    # Нам нужны только автофорварды
    if not getattr(msg, "is_automatic_forward", False):
        return

    origin = _extract_origin_channel_and_msg_id(msg)
    if origin is None:
        return

    channel_chat_id, channel_message_id = origin
    _discussion_map_put(
        channel_chat_id=channel_chat_id,
        channel_message_id=channel_message_id,
        discussion_chat_id=msg.chat.id,
        discussion_message_id=msg.message_id,
    )


async def handle_channel_photo_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    msg = update.effective_message
    if msg is None or msg.chat is None:
        return

    # Ограничение на список разрешённых каналов (опционально)
    allowed_ids = settings.allowed_channel_ids
    if allowed_ids is not None and msg.chat.id not in allowed_ids:
        return

    if not msg.photo:
        return

    # Не отвечаем несколько раз на один и тот же пост (на случай дублей апдейтов)
    if _mark_processed_channel_message(msg.chat.id, msg.message_id):
        return

    # Если это альбом (несколько картинок = media_group_id), отвечаем только один раз — на первую пришедшую картинку.
    media_group_id = getattr(msg, "media_group_id", None)
    if isinstance(media_group_id, str) and media_group_id:
        if _should_skip_media_group(media_group_id):
            return

    # Берём самое большое фото
    photo = msg.photo[-1]
    photo_file_id = photo.file_id

    # Запись в БД (для режима дневных опросов/статистики)
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(settings.daily_poll_timezone)
        post_date = (msg.date.astimezone(tz) if msg.date else datetime.now(tz)).date()
        await _record_post_for_poll_if_needed(
            context=context,
            channel_id=msg.chat.id,
            message_id=msg.message_id,
            post_date=post_date,
            photo_file_id=photo_file_id,
        )
    except Exception:
        logger.exception("Не удалось записать пост в БД для режима опросов")

    tg_file = await context.bot.get_file(photo_file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    original_caption = msg.caption

    try:
        caption = await generate_funny_caption(image_bytes=image_bytes, original_caption=original_caption)
    except TimewebAIError:
        logger.exception("Не удалось сгенерировать подпись через Timeweb AI")
        return

    async def _try_send_comment_with_retries() -> None:
        # Пытаемся дождаться, пока в linked-чате появится автофорвард (mapping).
        delays = [0.5, 1, 2, 4, 8, 12]
        for d in delays:
            ref = _discussion_map_get(msg.chat.id, msg.message_id)
            if ref is not None:
                try:
                    await context.bot.send_message(
                        chat_id=ref.discussion_chat_id,
                        text=caption,
                        reply_to_message_id=ref.discussion_message_id,
                        allow_sending_without_reply=True,
                    )
                except Exception:
                    logger.exception("Не удалось отправить комментарий в чат обсуждений")
                return
            await asyncio.sleep(d)

        logger.error(
            "Не найдено соответствие поста и сообщения в чате обсуждений. "
            "Проверьте: включены комментарии (linked chat), бот добавлен в чат обсуждений и видит автофорварды."
        )

    # Фоном, чтобы не задерживать обработку webhook
    asyncio.create_task(_try_send_comment_with_retries())

