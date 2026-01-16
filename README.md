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
- **`TIMEWEB_AI_USE_POST_CAPTION`**: использовать подпись поста как контекст (по умолчанию `true`)
- **`TIMEWEB_AI_EMOJI_RATIO`**: доля emoji‑реакций (0.0..1.0), по умолчанию `0.3`
- **`DATABASE_URL`**: строка подключения к Postgres (например, `postgresql://user:pass@host:5432/dbname`)
- **`DATABASE_HOST` / `DATABASE_NAME` / `DATABASE_USER` / `DATABASE_PASSWORD` / `DATABASE_PORT`**: альтернативные параметры подключения (если не используете `DATABASE_URL`)
- **`ADMIN_BASIC_USER` / `ADMIN_BASIC_PASSWORD`**: логин/пароль для админ‑страницы `/admin` (Basic Auth)

## Как работает привязка “пост → комментарии”

Telegram хранит комментарии к постам канала в отдельном чате обсуждений.  
Для того чтобы ответить “в комментарии к посту”, бот:

1) Ловит **автофорвард** поста в linked discussion group (`is_automatic_forward`)
2) Сохраняет соответствие «пост в канале → сообщение в чате обсуждений»
3) Публикует подпись reply’ем в linked chat

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

## Дневной опрос (режим poll)

Бот может публиковать **1 опрос в день** в заданных каналах (строгий whitelist).
Условия:

- окно публикации: по умолчанию **13:00–17:00 (MSK)**
- публикуется только если за день было **≥ 3 постов**
- варианты ответа генерируются через AI

Ключевые переменные:

- `DAILY_POLL_ENABLED=true`
- `DAILY_POLL_CHANNEL_IDS=-100...,-100...` (строгий список каналов)
- `DAILY_POLL_START_HOUR=13`, `DAILY_POLL_END_HOUR=17`
- `DAILY_POLL_MIN_POSTS=3`
- `DAILY_POLL_OPTIONS_COUNT=4`
- `DAILY_POLL_OPEN_SECONDS=3600` (если 0 или не задан — опрос бессрочный)
- `DAILY_POLL_QUESTIONS=Что тут происходит?|Что делать дальше?|...`

Для режима опросов нужен Postgres. Параметры подключения — в `env.example`.

## Структура базы данных (Postgres)

Таблица `posts` — хранит посты с картинками (для опросов и статистики):

- `id` BIGSERIAL PK
- `channel_id` BIGINT
- `message_id` BIGINT
- `post_date` DATE (в TZ `Europe/Moscow`)
- `photo_file_id` TEXT
- `created_at` TIMESTAMPTZ
- UNIQUE `(channel_id, message_id)`

Таблица `daily_poll` — состояние дневного опроса по каналу:

- `channel_id` BIGINT
- `poll_date` DATE
- `scheduled_at` TIMESTAMPTZ
- `posted_at` TIMESTAMPTZ NULL
- `skipped_at` TIMESTAMPTZ NULL
- `poll_message_id` BIGINT NULL
- `chosen_post_message_id` BIGINT NULL
- `question` TEXT
- `options` JSONB
- `last_error` TEXT NULL
- `last_error_at` TIMESTAMPTZ NULL
- PRIMARY KEY `(channel_id, poll_date)`

## Очистка таблиц

- `posts`: чистится **при первом посте нового дня** — удаляются записи старше 30 дней.
- `daily_poll`: текущая логика **не удаляет** строки автоматически, чтобы можно было смотреть историю.
  Если захотите авто‑чистку, можно добавить, например:
  - хранить 30–60 дней: `DELETE FROM daily_poll WHERE poll_date < CURRENT_DATE - INTERVAL '60 days'`.

## Админ‑страница и пагинация

Админ‑страница доступна по `/admin` и требует Basic Auth (`ADMIN_BASIC_USER` / `ADMIN_BASIC_PASSWORD`).
Пагинация поддерживается через query‑параметры:

- `limit` — количество строк (1..200, по умолчанию 50)
- `offset` — смещение (по умолчанию 0)

Пример:

```
/admin?limit=50&offset=100
```

Ручной запуск опроса:

- на странице `/admin` есть форма для запуска
- можно указать `channel_id` (из whitelist `DAILY_POLL_CHANNEL_IDS`)
