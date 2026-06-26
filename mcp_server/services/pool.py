"""Managed-DB connection pool for the MCP server (arch #253, finding 10#1).

Every MCP ``query``/``describe_table``/``list_tables`` and every artifact source
query previously opened a *fresh* psycopg TLS connection (``sslmode=require``),
so one agent turn against a 15-table schema could open 20+ serial TLS
connections. We pool the managed-DB connections per base DSN and reuse them
across queries, consistent with how the LangGraph checkpointer pools
(``apps/chat/checkpointer.py``).

The pool is keyed by the *base* connection identity (host/port/dbname/user) —
NOT the per-schema search_path — because every tenant/view schema lives in the
same managed database and differs only by ``search_path``/role, which callers
set per checkout. Pools are lazily created and cached per key for the lifetime
of the process.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# Connection-param keys that identify the base DB (NOT the per-schema options).
_BASE_KEYS = ("host", "port", "dbname", "user", "password", "sslmode")

# Cached pools keyed by base DSN tuple. One pool per managed DB per process.
_pools: dict[tuple, AsyncConnectionPool] = {}
_lock = asyncio.Lock()

# Bound the pool so a burst of concurrent queries can't exhaust managed-DB
# connection slots. Matches the checkpointer's sizing.
_POOL_MAX_SIZE = 10


def _base_conninfo(params: dict[str, Any]) -> str:
    """Build a libpq conninfo string from the base (schema-independent) params."""
    parts = []
    for key in _BASE_KEYS:
        val = params.get(key)
        if val in (None, ""):
            continue
        # Escape single quotes and backslashes per libpq conninfo rules.
        sval = str(val).replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"{key}='{sval}'")
    return " ".join(parts)


def _pool_key(params: dict[str, Any]) -> tuple:
    return tuple(params.get(k) for k in _BASE_KEYS)


async def get_pool(params: dict[str, Any]) -> AsyncConnectionPool:
    """Return a lazily-created, cached AsyncConnectionPool for these base params.

    The returned pool's connections carry NO per-schema search_path — callers
    set ``SET search_path``/``SET ROLE`` per checkout. Connections are opened
    with ``autocommit=True`` (the read path runs single statements) and
    ``prepare_threshold=0`` (PgBouncer-safe, matching the checkpointer pool).
    """
    key = _pool_key(params)
    pool = _pools.get(key)
    if pool is not None:
        return pool

    async with _lock:
        pool = _pools.get(key)
        if pool is not None:
            return pool
        conninfo = _base_conninfo(params)
        pool = AsyncConnectionPool(
            conninfo=conninfo,
            max_size=_POOL_MAX_SIZE,
            open=False,
            # check keeps the pool from handing out a connection that died
            # underneath it (RDS restart / idle timeout) — the long-lived-process
            # analogue of the worker's connection hygiene (arch #253, 08#0).
            check=AsyncConnectionPool.check_connection,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open(wait=True, timeout=10)
        _pools[key] = pool
        logger.info("Opened managed-DB connection pool (max_size=%d)", _POOL_MAX_SIZE)
        return pool


async def close_all_pools() -> None:
    """Close and drop all cached pools. Used in tests and on shutdown."""
    async with _lock:
        for pool in _pools.values():
            await pool.close()
        _pools.clear()
