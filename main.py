from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException, Request
from telegram.error import TelegramError
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

from bot_logic import handle_channel_photo_post
from config import Settings, get_settings_or_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def build_telegram_app(settings: Settings) -> Application:
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.CHANNEL,
            handle_channel_photo_post,
        )
    )
    return app


api = FastAPI()
telegram_app: Application | None = None


@api.get("/")
async def root() -> dict[str, str]:
    # Многие платформы по умолчанию проверяют именно "/" как healthcheck.
    return {"status": "ok"}


@api.get("/health")
async def health() -> dict[str, object]:
    settings, err = get_settings_or_error()
    return {
        "status": "ok",
        "config_ok": err is None,
        "config_error": err,
        "webhook_configured": bool(getattr(api.state, "webhook_configured", False)),
        "webhook_error": getattr(api.state, "webhook_error", None),
    }


@api.on_event("startup")
async def on_startup() -> None:
    global telegram_app

    settings, err = get_settings_or_error()
    api.state.webhook_configured = False
    api.state.webhook_error = None

    if err is not None:
        # Важно: не валим приложение, чтобы healthcheck контейнера прошёл,
        # а ошибка была видна в /health.
        logger.error("Config error. Set env vars in App Platform. Details: %s", err)
        return

    telegram_app = build_telegram_app(settings)
    await telegram_app.initialize()
    await telegram_app.start()

    # Настраиваем webhook на публичный URL приложения.
    # Важно: Telegram будет присылать заголовок X-Telegram-Bot-Api-Secret-Token,
    # мы его сверим в обработчике.
    try:
        await telegram_app.bot.set_webhook(
            url=settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret_token,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        api.state.webhook_configured = True
        logger.info("Webhook установлен: %s", settings.telegram_webhook_url)
    except TelegramError as e:
        # Не роняем приложение: healthcheck должен пройти, а пользователь сможет
        # поправить PUBLIC_BASE_URL/домен и сделать redeploy.
        api.state.webhook_error = str(e)
        logger.error("Не удалось установить webhook (%s): %s", settings.telegram_webhook_url, e)


@api.on_event("shutdown")
async def on_shutdown() -> None:
    global telegram_app
    if telegram_app is None:
        return

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
    settings, err = get_settings_or_error()
    if err is not None or settings is None or telegram_app is None:
        raise HTTPException(status_code=503, detail="service not configured")

    if path_secret != settings.webhook_path_secret:
        raise HTTPException(status_code=404, detail="not found")

    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret_token:
        raise HTTPException(status_code=403, detail="forbidden")

    data = await request.json()
    update = Update.de_json(data=data, bot=telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

