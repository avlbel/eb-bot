from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from html import escape

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from telegram.error import TelegramError
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

from bot_logic import handle_channel_photo_post, handle_discussion_auto_forward
from config import Settings, get_settings_or_error
from db import close_pool, create_pool
from poller import poller_loop, run_poll_once
from db import get_post
from timeweb_ai import TimewebAIError, generate_funny_caption


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
_instance_id = os.getenv("INSTANCE_ID") or str(uuid.uuid4())
print(f"BOOT INSTANCE_ID: {_instance_id}", flush=True)
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
    # Ловим автофорварды в linked discussion group, чтобы связать пост и "discussion message"
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            handle_discussion_auto_forward,
        )
    )
    return app


api = FastAPI()
telegram_app: Application | None = None
telegram_task: asyncio.Task | None = None
poller_task: asyncio.Task | None = None
basic_auth = HTTPBasic()


async def init_telegram_in_background(settings: Settings) -> None:
    """
    Инициализация Telegram может включать сетевые вызовы (getMe/setWebhook).
    Чтобы не мешать healthcheck'ам платформы, делаем это фоном.
    """
    global telegram_app, poller_task
    try:
        app = build_telegram_app(settings)
        await app.initialize()
        await app.start()
        telegram_app = app
        api.state.telegram_app = app
        # пробрасываем pool в bot_data, чтобы handlers могли писать в БД
        if getattr(api.state, "db_pool", None) is not None:
            app.bot_data["db_pool"] = api.state.db_pool

        # Стартуем poller после готовности telegram_app
        if poller_task is None:
            poller_task = asyncio.create_task(poller_loop(api.state))
            logger.info("Poller started")

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
    return PlainTextResponse(f"OK {_instance_id}")


@api.get("/health")
async def health() -> dict[str, object]:
    settings, err = get_settings_or_error()
    token_fp = None
    if settings is not None and err is None:
        token_fp = hashlib.sha256(settings.telegram_bot_token.encode("utf-8")).hexdigest()[:12]
    return {
        "status": "ok",
        "instance_id": _instance_id,
        "config_ok": err is None,
        "config_error": err,
        "bot_token_fp": token_fp,
        "webhook_configured": bool(getattr(api.state, "webhook_configured", False)),
        "webhook_error": getattr(api.state, "webhook_error", None),
    }


def _check_basic_auth(credentials: HTTPBasicCredentials, settings: Settings) -> None:
    if not settings.admin_basic_user or not settings.admin_basic_password:
        raise HTTPException(status_code=404, detail="not found")
    if (
        credentials.username != settings.admin_basic_user
        or credentials.password != settings.admin_basic_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@api.get("/admin", response_class=HTMLResponse)
async def admin_page(
    credentials: HTTPBasicCredentials = Depends(basic_auth),
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse:
    settings, err = get_settings_or_error()
    if err is not None or settings is None:
        raise HTTPException(status_code=503, detail="service not configured")

    _check_basic_auth(credentials, settings)

    pool = getattr(api.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db not configured")

    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))

    async with pool.acquire() as conn:
        posts = await conn.fetch(
            """
            SELECT channel_id, message_id, post_date, photo_file_id, created_at
            FROM posts
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        polls = await conn.fetch(
            """
            SELECT channel_id, poll_date, scheduled_at, posted_at, skipped_at,
                   poll_message_id, chosen_post_message_id, question, last_error, last_error_at
            FROM daily_poll
            ORDER BY poll_date DESC, channel_id
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )

    def _table(headers: list[str], rows: list[list[str]]) -> str:
        th = "".join(f"<th>{escape(h)}</th>" for h in headers)
        trs = []
        for r in rows:
            tds = "".join(f"<td>{escape(x)}</td>" for x in r)
            trs.append(f"<tr>{tds}</tr>")
        return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"

    posts_rows = [
        [
            str(r["channel_id"]),
            str(r["message_id"]),
            str(r["post_date"]),
            str(r["photo_file_id"] or ""),
            str(r["created_at"]),
        ]
        for r in posts
    ]
    polls_rows = [
        [
            str(r["channel_id"]),
            str(r["poll_date"]),
            str(r["scheduled_at"]),
            str(r["posted_at"] or ""),
            str(r["skipped_at"] or ""),
            str(r["poll_message_id"] or ""),
            str(r["chosen_post_message_id"] or ""),
            str(r["question"] or ""),
            str(r["last_error"] or ""),
            str(r["last_error_at"] or ""),
        ]
        for r in polls
    ]

    html = f"""
    <html>
      <head>
        <meta charset="utf-8"/>
        <title>EB Bot Admin</title>
        <style>
          body {{ font-family: Arial, sans-serif; margin: 20px; }}
          table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
          th, td {{ border: 1px solid #ddd; padding: 6px 8px; font-size: 12px; }}
          th {{ background: #f4f4f4; text-align: left; }}
          .actions {{ margin: 8px 0 24px; }}
        </style>
      </head>
      <body>
        <div class="actions">
          <form method="post" action="/admin/poll/run">
            <label>Channel ID: <input name="channel_id" placeholder="-100123..." /></label>
            <button type="submit">Запустить опрос вручную</button>
          </form>
          <form method="post" action="/admin/post/regenerate">
            <label>Channel ID: <input name="channel_id" placeholder="-100123..." /></label>
            <label>Message ID: <input name="message_id" placeholder="12345" /></label>
            <button type="submit">Пересоздать подпись к посту</button>
          </form>
        </div>
        <h2>posts</h2>
        {_table(["channel_id","message_id","post_date","photo_file_id","created_at"], posts_rows)}
        <h2>daily_poll</h2>
        {_table(["channel_id","poll_date","scheduled_at","posted_at","skipped_at","poll_message_id","chosen_post_message_id","question","last_error","last_error_at"], polls_rows)}
        <p>limit={limit} offset={offset}</p>
      </body>
    </html>
    """
    return HTMLResponse(html)


@api.post("/admin/poll/run")
async def admin_run_poll(
    credentials: HTTPBasicCredentials = Depends(basic_auth),
    channel_id: str | None = Form(default=None),
) -> dict[str, object]:
    settings, err = get_settings_or_error()
    if err is not None or settings is None:
        raise HTTPException(status_code=503, detail="service not configured")
    _check_basic_auth(credentials, settings)

    force_channel_id = None
    if channel_id:
        try:
            force_channel_id = int(channel_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid channel_id")

    result = await run_poll_once(api.state, force=True, force_channel_id=force_channel_id)
    return {"ok": True, "result": result}


@api.post("/admin/post/regenerate")
async def admin_regenerate_post_caption(
    credentials: HTTPBasicCredentials = Depends(basic_auth),
    channel_id: str | None = Form(default=None),
    message_id: str | None = Form(default=None),
) -> dict[str, object]:
    settings, err = get_settings_or_error()
    if err is not None or settings is None:
        raise HTTPException(status_code=503, detail="service not configured")
    _check_basic_auth(credentials, settings)

    if not channel_id or not message_id:
        raise HTTPException(status_code=400, detail="channel_id and message_id are required")

    try:
        channel_id_int = int(channel_id)
        message_id_int = int(message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid channel_id or message_id")

    pool = getattr(api.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="db not configured")

    post = await get_post(pool, channel_id_int, message_id_int)
    if not post:
        return {"ok": False, "reason": "post_not_found"}

    photo_file_id = post.get("photo_file_id")
    discussion_chat_id = post.get("discussion_chat_id")
    discussion_message_id = post.get("discussion_message_id")
    if not photo_file_id:
        return {"ok": False, "reason": "no_photo_file_id"}
    if not discussion_chat_id or not discussion_message_id:
        return {"ok": False, "reason": "no_discussion_mapping"}

    app = getattr(api.state, "telegram_app", None)
    if app is None:
        return {"ok": False, "reason": "telegram_app_not_ready"}

    try:
        tg_file = await app.bot.get_file(photo_file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
        caption = await generate_funny_caption(image_bytes=image_bytes, original_caption=None)
        await app.bot.send_message(
            chat_id=int(discussion_chat_id),
            text=caption,
            reply_to_message_id=int(discussion_message_id),
            allow_sending_without_reply=True,
        )
    except TimewebAIError as e:
        return {"ok": False, "reason": "ai_failed", "error": str(e)}
    except TelegramError:
        logger.exception("Admin regenerate failed to send comment")
        return {"ok": False, "reason": "send_failed"}

    return {"ok": True}


@api.on_event("startup")
async def on_startup() -> None:
    global telegram_task, poller_task

    settings, err = get_settings_or_error()
    api.state.webhook_configured = False
    api.state.webhook_error = None

    if err is not None:
        # Важно: не валим приложение, чтобы healthcheck контейнера прошёл,
        # а ошибка была видна в /health.
        logger.error("Config error. Set env vars in App Platform. Details: %s", err)
        return

    # DB pool (для режима опросов/статистики)
    if settings.database_dsn:
        try:
            pool = await create_pool(settings.database_dsn)
            api.state.db_pool = pool
        except Exception:
            logger.exception("DB init failed; daily poll disabled")
            api.state.db_pool = None
    else:
        api.state.db_pool = None

    # Логируем fingerprint токена, чтобы было видно, какой токен реально подхватился из env.
    token_fp = hashlib.sha256(settings.telegram_bot_token.encode("utf-8")).hexdigest()[:12]
    logger.info("Bot token fingerprint: %s", token_fp)

    # Фоновая инициализация Telegram, чтобы не блокировать readiness.
    telegram_task = asyncio.create_task(init_telegram_in_background(settings))


@api.on_event("shutdown")
async def on_shutdown() -> None:
    global telegram_app
    global telegram_task
    global poller_task

    if telegram_task is not None:
        telegram_task.cancel()
        telegram_task = None

    if poller_task is not None:
        poller_task.cancel()
        poller_task = None

    if telegram_app is None:
        # Закрываем DB pool, если есть
        pool = getattr(api.state, "db_pool", None)
        if pool is not None:
            await close_pool(pool)
        return

    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        logger.exception("delete_webhook failed (ignore)")

    await telegram_app.stop()
    await telegram_app.shutdown()

    pool = getattr(api.state, "db_pool", None)
    if pool is not None:
        await close_pool(pool)


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

