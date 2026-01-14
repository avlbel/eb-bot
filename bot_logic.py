from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import get_settings
from timeweb_ai import TimewebAIError, generate_funny_caption

logger = logging.getLogger(__name__)


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
        discussion_message = await context.bot.get_discussion_message(
            chat_id=msg.chat.id,
            message_id=msg.message_id,
        )
    except Exception:
        logger.exception(
            "Не удалось получить discussion message. "
            "Проверьте, что у канала включены комментарии (linked chat) и бот имеет доступ."
        )
        return

    try:
        await context.bot.send_message(
            chat_id=discussion_message.chat.id,
            text=caption,
            reply_to_message_id=discussion_message.message_id,
            allow_sending_without_reply=True,
        )
    except Exception:
        logger.exception("Не удалось отправить комментарий в чат обсуждений")

