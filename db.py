from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import asyncpg
import logging


logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS posts (
    id BIGSERIAL PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    post_date DATE NOT NULL,
    photo_file_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (channel_id, message_id)
);

ALTER TABLE posts
    ADD COLUMN IF NOT EXISTS photo_file_id TEXT;

CREATE INDEX IF NOT EXISTS idx_posts_channel_date ON posts(channel_id, post_date);

CREATE TABLE IF NOT EXISTS daily_poll (
    channel_id BIGINT NOT NULL,
    poll_date DATE NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    posted_at TIMESTAMPTZ,
    skipped_at TIMESTAMPTZ,
    poll_message_id BIGINT,
    chosen_post_message_id BIGINT,
    question TEXT,
    options JSONB,
    PRIMARY KEY (channel_id, poll_date)
);
"""


async def create_pool(dsn: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    logger.info("Postgres connected and schema ensured")
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()


async def maybe_cleanup_old_posts(pool: asyncpg.Pool, today: date, days: int = 30) -> None:
    """
    Очистка старых постов при первом посте нового дня (глобально).
    Если в БД уже есть посты за today — чистку не делаем.
    """
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM posts WHERE post_date = $1 LIMIT 1",
            today,
        )
        if exists:
            return
        await conn.execute(
            "DELETE FROM posts WHERE created_at < (NOW() - make_interval(days => $1))",
            int(days),
        )


async def record_post(
    pool: asyncpg.Pool,
    channel_id: int,
    message_id: int,
    post_date: date,
    photo_file_id: str | None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO posts(channel_id, message_id, post_date, photo_file_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (channel_id, message_id) DO NOTHING
            """,
            channel_id,
            message_id,
            post_date,
            photo_file_id,
        )


async def ensure_daily_poll(pool: asyncpg.Pool, channel_id: int, poll_date: date, scheduled_at: datetime) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO daily_poll(channel_id, poll_date, scheduled_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (channel_id, poll_date) DO NOTHING
            """,
            channel_id,
            poll_date,
            scheduled_at,
        )


async def get_due_polls(pool: asyncpg.Pool, now_utc: datetime) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM daily_poll
            WHERE posted_at IS NULL
              AND skipped_at IS NULL
              AND scheduled_at <= $1
            """,
            now_utc,
        )


async def count_posts_for_date(pool: asyncpg.Pool, channel_id: int, poll_date: date) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM posts WHERE channel_id = $1 AND post_date = $2",
            channel_id,
            poll_date,
        )


async def pick_random_post(pool: asyncpg.Pool, channel_id: int, poll_date: date) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT message_id, photo_file_id
            FROM posts
            WHERE channel_id = $1 AND post_date = $2
            ORDER BY RANDOM()
            LIMIT 1
            """,
            channel_id,
            poll_date,
        )


async def mark_poll_posted(
    pool: asyncpg.Pool,
    channel_id: int,
    poll_date: date,
    poll_message_id: int,
    chosen_post_message_id: int,
    question: str,
    options: list[str],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE daily_poll
            SET posted_at = NOW(),
                poll_message_id = $3,
                chosen_post_message_id = $4,
                question = $5,
                options = $6::jsonb
            WHERE channel_id = $1 AND poll_date = $2
            """,
            channel_id,
            poll_date,
            poll_message_id,
            chosen_post_message_id,
            question,
            json.dumps(options, ensure_ascii=False),
        )


async def mark_poll_skipped(pool: asyncpg.Pool, channel_id: int, poll_date: date) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE daily_poll
            SET skipped_at = NOW()
            WHERE channel_id = $1 AND poll_date = $2
            """,
            channel_id,
            poll_date,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

