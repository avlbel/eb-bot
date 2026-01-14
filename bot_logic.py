from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ContextTypes

from config import get_settings
from timeweb_ai import TimewebAIError, generate_funny_caption

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

    # Ограничение на конкретный канал (опционально)
    if settings.allowed_channel_id is not None and msg.chat.id != settings.allowed_channel_id:
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
    tg_file = await context.bot.get_file(photo.file_id)
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

