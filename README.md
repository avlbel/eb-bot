## Что делает бот

Бот получает **картинку из нового поста в Telegram‑канале**, генерирует **смешную подпись** через **AI‑агента в timeweb.cloud** и публикует подпись **в комментариях к посту** (в привязанном чате обсуждений, reply к “discussion message”).

## Требования в Telegram

- **У канала должны быть включены комментарии** (привязан чат обсуждений / linked chat).
- Бот должен быть **администратором в канале** (чтобы получать `channel_post`).
- Бот должен иметь право **писать сообщения** в чате обсуждений (обычно достаточно быть участником/админом).
- Если бот “не видит” посты: проверьте, что он добавлен именно в **канал** (не только в чат обсуждений) и имеет права **Read messages / Post messages**.

## Переменные окружения

Список — в `env.example`.

Ключевые:

- **`TELEGRAM_BOT_TOKEN`**: токен от `@BotFather`
- **`PUBLIC_BASE_URL`**: публичный URL приложения в Timeweb App Platform (HTTPS)
- **`TELEGRAM_WEBHOOK_PATH_SECRET`**: секрет в пути вебхука
- **`TELEGRAM_WEBHOOK_SECRET_TOKEN`**: секрет для заголовка Telegram `X-Telegram-Bot-Api-Secret-Token`
- **`TIMEWEB_AI_API_KEY`**, **`TIMEWEB_AI_MODEL`**, **`TIMEWEB_AI_BASE_URL`**
- **`TIMEWEB_AI_CHAT_PATH`**: путь к endpoint chat‑completions (по умолчанию `/v1/chat/completions`)
- **`TELEGRAM_ALLOWED_CHANNEL_ID` / `TELEGRAM_ALLOWED_CHANNEL_IDS`**: ограничение на список разрешённых каналов (через запятую)

## Как работает привязка “пост → комментарии”

Telegram хранит комментарии к постам канала в отдельном чате обсуждений.  
Для того чтобы ответить “в комментарии к посту”, бот делает:

1) `getDiscussionMessage(channel_id, channel_message_id)` — получает соответствующее сообщение в чате обсуждений  
2) `sendMessage(discussion_chat_id, reply_to_message_id=discussion_message_id)` — публикует подпись reply’ем

## Локальный запуск

1) Установить зависимости:

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2) Создать `.env` **локально** по образцу `env.example` (файл не коммитить).

3) Запуск:

```bash
uvicorn main:api --host 0.0.0.0 --port 8080
```

Локально webhook работать не будет без публичного HTTPS URL (нужен ngrok/Cloudflare Tunnel), поэтому удобнее сразу тестировать в Timeweb App Platform.

## Деплой в timeweb.cloud App Platform

1) **Создайте репозиторий** (GitHub/GitLab) и залейте туда этот проект.
2) В панели timeweb.cloud откройте **App Platform → Создать приложение**.
3) Выберите **Deploy from repository** и укажите репозиторий/ветку.
4) Тип приложения: **Dockerfile** (проект уже содержит `Dockerfile`).
5) В настройках приложения добавьте **Environment variables** из `env.example`:
   - обязательно укажите `PUBLIC_BASE_URL` — это домен/URL, который выдаст App Platform вашему приложению
   - `PORT` обычно выставляет платформа сама (мы его поддерживаем)
6) Дождитесь деплоя, проверьте `GET /health` по вашему публичному домену.

После старта приложение **само вызовет `setWebhook`** на `PUBLIC_BASE_URL/webhook/<TELEGRAM_WEBHOOK_PATH_SECRET>`.

## Частые проблемы

- **`getDiscussionMessage` падает**: у канала не включены комментарии, либо бот не имеет доступа к linked chat.
- **Нет апдейтов**: бот не админ канала или webhook не установлен (проверьте логи приложения и что `PUBLIC_BASE_URL` верный).
- **403 на webhook**: не совпадает `TELEGRAM_WEBHOOK_SECRET_TOKEN` (Telegram присылает его в заголовке).

## Важно про Timeweb AI‑агента

В `timeweb_ai.py` используется **OpenAI‑совместимый** вызов:

- `POST {TIMEWEB_AI_BASE_URL}{TIMEWEB_AI_CHAT_PATH}`
- `Authorization: Bearer {TIMEWEB_AI_API_KEY}`
- `model = TIMEWEB_AI_MODEL`
- картинка отправляется как `data:image/...;base64,...` (vision)

Если ваш AI‑агент Timeweb использует другой URL/формат — скажите мне ссылку на вашу страницу API/пример curl, и я подгоню клиент под точный контракт.

