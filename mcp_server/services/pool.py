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

Known limitation, deliberately not addressed here: an ``AsyncConnectionPool``'s
worker tasks belong to the event loop that opened it, but the cache is keyed by
DSN alone, so a pool opened on one loop can be handed to another. That is
reachable — the ASGI/worker loop reaches ``get_pool`` through
``metadata``/``execute_query`` while ``async_to_sync`` (semantic catalog's
``load_physical_tables``, called from ``apps.workspaces.tasks``) builds its own
loop and calls the same code. Fixing it needs per-loop pools *plus* active
closure when a loop finishes; per-loop pools alone multiply open connections by
the number of loops, which exhausts the server's connection slots. That belongs
in its own change.
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

# Fragile by construction: an asyncio.Lock binds to the first loop that *awaits*
# on it and raises for every other loop. Safe today only because the uncontended
# path acquires without awaiting, so it never binds. Contention from two loops
# would surface as a RuntimeError here rather than silent corruption.
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
    if pool is not None and not pool.closed:
        return pool

    async with _lock:
        pool = _pools.get(key)
        if pool is not None:
            if not pool.closed:
                return pool
            # Never hand back a closed pool: that is what turned one failed
            # teardown into PoolClosed for the rest of the process.
            _pools.pop(key, None)
            await _close_evicted(pool)

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


async def _close_evicted(pool: AsyncConnectionPool) -> None:
    """Close a pool that has already been dropped from the cache.

    psycopg marks a pool closed *before* it awaits its workers, so a close that
    raises still leaves an unusable pool behind. Callers must therefore evict
    first and close second, or one failed close poisons the cache for the rest
    of the process.
    """
    await pool.close()


async def close_all_pools() -> None:
    """Close and drop all cached pools. Used in tests and on shutdown."""
    async with _lock:
        pools = list(_pools.values())
        # Clear before closing: a pool that fails to close is already unusable,
        # and leaving it cached makes every later get_pool() return a dead pool.
        _pools.clear()

    # Isolate per-pool failures, or the first raising close would strand every
    # pool behind it — already evicted, so unreachable and leaked for the process.
    results = await asyncio.gather(*(_close_evicted(p) for p in pools), return_exceptions=True)
    cancelled = None
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            cancelled = result
        elif isinstance(result, BaseException):
            logger.warning("Failed to close managed-DB pool: %s", result)
    if cancelled is not None:
        # Never swallow cancellation: the caller's task is being torn down.
        raise cancelled
