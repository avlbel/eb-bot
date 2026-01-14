from __future__ import annotations

import logging

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from config import get_settings
from timeweb_ai import TimewebAIError, generate_funny_caption

logger = logging.getLogger(__name__)

class TelegramAPIError(RuntimeError):
    pass


async def _get_discussion_message_fallback(
    bot_token: str,
    channel_chat_id: int,
    channel_message_id: int,
) -> dict:
    """
    Фолбэк на случай, если библиотека python-telegram-bot не содержит метода get_discussion_message.
    Вызывает Telegram Bot API напрямую: getDiscussionMessage.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getDiscussionMessage"
    data = {"chat_id": channel_chat_id, "message_id": channel_message_id}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, data=data)
        payload = r.json()

    if not payload.get("ok"):
        raise TelegramAPIError(str(payload))
    return payload["result"]


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

    # Комментарии = сообщение в linked discussion group, ответом на "discussion message"
    try:
        if hasattr(context.bot, "get_discussion_message"):
            discussion_message = await context.bot.get_discussion_message(
                chat_id=msg.chat.id,
                message_id=msg.message_id,
            )
            discussion_chat_id = discussion_message.chat.id
            discussion_message_id = discussion_message.message_id
        else:
            # PTB-версии без обёртки — используем прямой вызов Telegram API
            dm = await _get_discussion_message_fallback(
                bot_token=settings.telegram_bot_token,
                channel_chat_id=msg.chat.id,
                channel_message_id=msg.message_id,
            )
            discussion_chat_id = int(dm["chat"]["id"])
            discussion_message_id = int(dm["message_id"])
    except Exception:
        logger.exception(
            "Не удалось получить discussion message. "
            "Проверьте, что у канала включены комментарии (linked chat) и бот имеет доступ."
        )
        return

    try:
        await context.bot.send_message(
            chat_id=discussion_chat_id,
            text=caption,
            reply_to_message_id=discussion_message_id,
            allow_sending_without_reply=True,
        )
    except Exception:
        logger.exception("Не удалось отправить комментарий в чат обсуждений")

