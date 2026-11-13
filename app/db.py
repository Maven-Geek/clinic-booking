from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from . import config

_pool: asyncpg.Pool | None = None

# libpq-only options that asyncpg would otherwise forward to the server as settings and fail on.
# Neon's copy-paste connection string includes channel_binding=require, for example.
_UNSUPPORTED_DSN_OPTIONS = {"channel_binding", "pgbouncer", "connection_limit", "pool_timeout"}


def _clean_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in _UNSUPPORTED_DSN_OPTIONS]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def connect() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        _clean_dsn(config.DATABASE_URL),
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        # Serverless Postgres (Neon) suspends after a few idle minutes and drops open
        # connections. Closing our idle connections well before that means the next
        # request opens a fresh one instead of failing on a dead socket.
        max_inactive_connection_lifetime=60,
        # Hosted Postgres often sits behind PgBouncer in transaction mode (Neon's pooled
        # endpoint, Supabase's pooler), which breaks cached prepared statements.
        statement_cache_size=0,
    )
    schema = (Path(__file__).parent / "schema.sql").read_text()
    async with _pool.acquire() as conn:
        # Serialise schema creation in case more than one worker boots at once.
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7243001)")
            await conn.execute(schema)


async def disconnect() -> None:
    if _pool is not None:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool
