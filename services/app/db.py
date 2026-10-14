"""Thin async Postgres layer (psycopg 3). One pool shared by the app and LangGraph."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .config import settings

log = logging.getLogger("db")
_pool: AsyncConnectionPool | None = None

POOL_KWARGS = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}


async def init_pool(max_size: int = 10) -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=max_size,
                                    kwargs=POOL_KWARGS, open=False)
        for attempt in range(60):
            try:
                await _pool.open(wait=True, timeout=5)
                break
            except Exception as e:  # db still starting
                log.warning("db not ready (%s), retrying", e)
                await asyncio.sleep(2)
    return _pool


def pool() -> AsyncConnectionPool:
    assert _pool is not None, "pool not initialised"
    return _pool


def J(value):
    return Jsonb(value)


async def fetch(sql: str, *args) -> list[dict]:
    async with pool().connection() as conn:
        cur = await conn.execute(sql, args or None)
        return await cur.fetchall() if cur.description else []


async def fetchrow(sql: str, *args) -> dict | None:
    async with pool().connection() as conn:
        cur = await conn.execute(sql, args or None)
        return await cur.fetchone() if cur.description else None


async def fetchval(sql: str, *args):
    row = await fetchrow(sql, *args)
    return next(iter(row.values())) if row else None


async def execute(sql: str, *args) -> int:
    async with pool().connection() as conn:
        cur = await conn.execute(sql, args or None)
        return cur.rowcount


@asynccontextmanager
async def tx():
    async with pool().connection() as conn:
        async with conn.transaction():
            yield conn


@asynccontextmanager
async def scoped(team_ids: list):
    """Transaction running as the RLS-restricted role with the caller's team scope."""
    async with tx() as conn:
        await conn.execute("SET LOCAL ROLE app_scoped", prepare=False)
        await conn.execute("SELECT set_config('app.team_ids', %s, true)",
                           (",".join(str(t) for t in team_ids),))
        yield conn


async def migrate():
    sql = (Path(__file__).parent / "schema.sql").read_text()
    async with tx() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(7001)")
        await conn.execute(sql, prepare=False)
