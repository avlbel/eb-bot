from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

from bot_logic import handle_channel_photo_post
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def build_telegram_app() -> Application:
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.CHANNEL,
            handle_channel_photo_post,
        )
    )
    return app


telegram_app = build_telegram_app()
api = FastAPI()


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.on_event("startup")
async def on_startup() -> None:
    await telegram_app.initialize()
    await telegram_app.start()

    # Настраиваем webhook на публичный URL приложения.
    # Важно: Telegram будет присылать заголовок X-Telegram-Bot-Api-Secret-Token,
    # мы его сверим в обработчике.
    await telegram_app.bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret_token,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Webhook установлен: %s", settings.telegram_webhook_url)


@api.on_event("shutdown")
async def on_shutdown() -> None:
    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        logger.exception("delete_webhook failed (ignore)")

    await telegram_app.stop()
    await telegram_app.shutdown()


@api.post("/webhook/{path_secret}")
async def telegram_webhook(
    path_secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if path_secret != settings.webhook_path_secret:
        raise HTTPException(status_code=404, detail="not found")

    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret_token:
        raise HTTPException(status_code=403, detail="forbidden")

    data = await request.json()
    update = Update.de_json(data=data, bot=telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

