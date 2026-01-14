from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from telegram.error import TelegramError
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

from bot_logic import handle_channel_photo_post
from config import Settings, get_settings_or_error


class _RedactTelegramTokenFilter(logging.Filter):
    _re = re.compile(r"(https://api\.telegram\.org/)?bot\d+:[A-Za-z0-9_-]+")

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            return True

        redacted = self._re.sub("bot<redacted>", msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
# Редактируем логи, чтобы токены Telegram не утекали (URL httpx, тексты исключений и т.д.)
for h in logging.getLogger().handlers:
    h.addFilter(_RedactTelegramTokenFilter())
# Не логируем HTTP-запросы библиотеки Telegram/httpx с URL, содержащим токен бота.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").disabled = True
logging.getLogger("httpcore").disabled = True

# Диагностика самого раннего старта: какой TELEGRAM_BOT_TOKEN реально попал в env контейнера.
_raw_boot_token = os.getenv("TELEGRAM_BOT_TOKEN")
if _raw_boot_token:
    _boot_fp = hashlib.sha256(_raw_boot_token.encode("utf-8")).hexdigest()[:12]
    print(f"BOOT TELEGRAM_BOT_TOKEN fingerprint: {_boot_fp}", flush=True)
else:
    print("BOOT TELEGRAM_BOT_TOKEN fingerprint: <missing>", flush=True)


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
telegram_task: asyncio.Task | None = None


async def init_telegram_in_background(settings: Settings) -> None:
    """
    Инициализация Telegram может включать сетевые вызовы (getMe/setWebhook).
    Чтобы не мешать healthcheck'ам платформы, делаем это фоном.
    """
    global telegram_app
    try:
        app = build_telegram_app(settings)
        await app.initialize()
        await app.start()
        telegram_app = app

        try:
            await telegram_app.bot.set_webhook(
                url=settings.telegram_webhook_url,
                secret_token=settings.telegram_webhook_secret_token,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            api.state.webhook_configured = True
            api.state.webhook_error = None
            logger.info("Webhook установлен: %s", settings.telegram_webhook_url)
        except TelegramError as e:
            api.state.webhook_configured = False
            api.state.webhook_error = str(e)
            logger.error("Не удалось установить webhook (%s): %s", settings.telegram_webhook_url, e)
    except Exception:
        logger.exception("Ошибка инициализации Telegram (ignore, сервис останется жив)")


@api.get("/")
async def root() -> PlainTextResponse:
    # Многие платформы по умолчанию проверяют именно "/" как healthcheck.
    return PlainTextResponse("OK")


@api.get("/health")
async def health() -> dict[str, object]:
    settings, err = get_settings_or_error()
    token_fp = None
    if settings is not None and err is None:
        token_fp = hashlib.sha256(settings.telegram_bot_token.encode("utf-8")).hexdigest()[:12]
    return {
        "status": "ok",
        "config_ok": err is None,
        "config_error": err,
        "bot_token_fp": token_fp,
        "webhook_configured": bool(getattr(api.state, "webhook_configured", False)),
        "webhook_error": getattr(api.state, "webhook_error", None),
    }


@api.on_event("startup")
async def on_startup() -> None:
    global telegram_task

    settings, err = get_settings_or_error()
    api.state.webhook_configured = False
    api.state.webhook_error = None

    if err is not None:
        # Важно: не валим приложение, чтобы healthcheck контейнера прошёл,
        # а ошибка была видна в /health.
        logger.error("Config error. Set env vars in App Platform. Details: %s", err)
        return

    # Логируем fingerprint токена, чтобы было видно, какой токен реально подхватился из env.
    token_fp = hashlib.sha256(settings.telegram_bot_token.encode("utf-8")).hexdigest()[:12]
    logger.info("Bot token fingerprint: %s", token_fp)

    # Фоновая инициализация Telegram, чтобы не блокировать readiness.
    telegram_task = asyncio.create_task(init_telegram_in_background(settings))


@api.on_event("shutdown")
async def on_shutdown() -> None:
    global telegram_app
    global telegram_task

    if telegram_task is not None:
        telegram_task.cancel()
        telegram_task = None

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

