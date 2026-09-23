"""Managed-DB connection pools for the MCP server (arch #253, finding 10#1).

Every MCP ``query``/``describe_table``/``list_tables`` and every artifact source
query previously opened a *fresh* psycopg TLS connection (``sslmode=require``),
so one agent turn against a 15-table schema could open 20+ serial TLS
connections. We pool the managed-DB connections per base DSN and reuse them
across queries, consistent with how the LangGraph checkpointer pools
(``apps/chat/checkpointer.py``).

The pool is keyed by the *base* connection identity (host/port/dbname/user) —
NOT the per-schema search_path — because every tenant/view schema lives in the
same managed database and differs only by ``search_path``/role, which callers
set per checkout.

Pools are also owned by the event loop that opened them. An
``AsyncConnectionPool``'s workers are tasks on that loop, so a pool handed to
another loop has nobody servicing it, and once its loop ends it reports
``closed == False`` while every checkout that needs a worker hangs. Several
loops really do reach ``get_pool``: the ASGI/worker loop, plus the fresh loop
``async_to_sync`` builds for each call from a plain thread (semantic catalog's
``load_physical_tables``, called from ``apps.workspaces.tasks``). So:

- each loop gets its own pool, and ``get_pool`` never returns another loop's;
- a pool is closed on its own loop as that loop shuts down (``asyncio.run``,
  ``asyncio.Runner`` and pytest-asyncio all run ``shutdown_asyncgens``), so
  per-call loops do not accumulate open connections;
- live pools are capped process-wide (``_MAX_POOLS`` × ``_POOL_MAX_SIZE``
  connections), because production and staging share one RDS instance with
  tight ``max_connections``. At the cap, a new loop waits for a slot rather than
  evicting a live loop's pool — eviction is what made two live loops thrash.

Pools only learn their loop is finished from its shutdown. A loop that is
stopped but never closed, or a pool first opened after ``shutdown_asyncgens``
already ran, keeps its slot until a later sweep sees the loop closed (the
sweep runs on every new-pool request and after each test).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import weakref
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool, PoolTimeout

logger = logging.getLogger(__name__)

# Connection-param keys that identify the base DB (NOT the per-schema options).
_BASE_KEYS = ("host", "port", "dbname", "user", "password", "sslmode")

_POOL_MIN_SIZE = 1
# Bound each pool so a burst of concurrent queries can't exhaust managed-DB
# connection slots. Matches the checkpointer's sizing.
_POOL_MAX_SIZE = 10
_POOL_MAX_IDLE_SECONDS = 60.0
# One slot per concurrently live loop: the ASGI/worker loop plus any
# async_to_sync loops running at once (bounded today by worker concurrency).
# Raising worker concurrency past this turns slot waits into PoolTimeout.
_MAX_POOLS = 4
_SLOT_WAIT_SECONDS = 30.0
_SLOT_POLL_SECONDS = 0.05


@dataclass
class _Entry:
    pool: AsyncConnectionPool
    loop: asyncio.AbstractEventLoop
    # Holding the generator keeps the loop-shutdown hook registered for this pool.
    lifetime: AsyncGenerator[None, None]


# (base DSN tuple, owning loop)
_PoolKey = tuple[tuple, asyncio.AbstractEventLoop]

# Mutated from several threads' loops; guarded by _state_lock.
_pools: dict[_PoolKey, _Entry] = {}
_opening = 0
_state_lock = threading.Lock()
_open_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


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


def _open_lock(loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    with _state_lock:
        lock = _open_locks.get(loop)
        if lock is None:
            lock = _open_locks[loop] = asyncio.Lock()
        return lock


async def get_pool(params: dict[str, Any]) -> AsyncConnectionPool:
    """Return this event loop's lazily-created, cached pool for these base params.

    The returned pool's connections carry NO per-schema search_path — callers
    set ``SET search_path``/``SET ROLE`` per checkout. Connections are opened
    with ``autocommit=True`` (the read path runs single statements) and
    ``prepare_threshold=0`` (PgBouncer-safe, matching the checkpointer pool).
    """
    loop = asyncio.get_running_loop()
    key: _PoolKey = (_pool_key(params), loop)
    entry = _pools.get(key)
    if entry is not None and not entry.pool.closed:
        return entry.pool

    # One deadline per caller, including the wait for the loop's open lock, so
    # callers queued behind a saturated cap don't each add a full slot wait.
    deadline = time.monotonic() + _SLOT_WAIT_SECONDS
    lock = _open_lock(loop)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_SLOT_WAIT_SECONDS)
    except TimeoutError:
        raise PoolTimeout("timed out waiting to open a managed-DB pool") from None
    try:
        return await _get_or_open_pool(key, params, deadline)
    finally:
        lock.release()


async def _get_or_open_pool(key: _PoolKey, params: dict[str, Any], deadline: float):
    entry = _pools.get(key)
    if entry is not None:
        if not entry.pool.closed:
            return entry.pool
        # Never hand back a closed pool: that is what turned one failed
        # teardown into PoolClosed for the rest of the process.
        await entry.lifetime.aclose()

    await _reserve_slot(deadline)
    try:
        pool = await _open_pool(params)
    except BaseException:
        _release_slot()
        raise

    lifetime = _close_on_loop_shutdown(key, pool)
    try:
        # First iteration registers the generator with this loop, whose shutdown
        # (asyncio.run / Runner) then closes the pool while the loop still runs.
        await anext(lifetime)
    except BaseException:
        _release_slot()
        await _close_pool(pool)
        raise
    _commit_slot(key, _Entry(pool=pool, loop=key[1], lifetime=lifetime))
    logger.info("Opened managed-DB connection pool (max_size=%d)", _POOL_MAX_SIZE)
    return pool


async def _open_pool(params: dict[str, Any]) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        conninfo=_base_conninfo(params),
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        max_idle=_POOL_MAX_IDLE_SECONDS,
        open=False,
        # check keeps the pool from handing out a connection that died
        # underneath it (RDS restart / idle timeout) — the long-lived-process
        # analogue of the worker's connection hygiene (arch #253, 08#0).
        check=AsyncConnectionPool.check_connection,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    try:
        await pool.open(wait=True, timeout=10)
    except BaseException:
        await pool.close()
        raise
    return pool


async def _reserve_slot(deadline: float) -> None:
    global _opening
    while True:
        release_pools_of_finished_loops()
        with _state_lock:
            if len(_pools) + _opening < _MAX_POOLS:
                _opening += 1
                return
        if time.monotonic() >= deadline:
            with _state_lock:
                idle_loops = sum(1 for entry in _pools.values() if not entry.loop.is_running())
            # A loop that stopped without being closed keeps its slot: nothing
            # signals that it is finished. Name that case so it is diagnosable.
            logger.warning(
                "Managed-DB pool cap reached: %d pools cached, %d on loops not running",
                len(_pools),
                idle_loops,
            )
            raise PoolTimeout(
                f"all {_MAX_POOLS} managed-DB pool slots are held by other event loops"
            )
        await asyncio.sleep(_SLOT_POLL_SECONDS)


def _release_slot() -> None:
    global _opening
    with _state_lock:
        _opening -= 1


def _commit_slot(key: _PoolKey, entry: _Entry) -> None:
    global _opening
    with _state_lock:
        _opening -= 1
        _pools[key] = entry


def _forget(key: _PoolKey, pool: AsyncConnectionPool) -> None:
    with _state_lock:
        entry = _pools.get(key)
        if entry is not None and entry.pool is pool:
            del _pools[key]


async def _close_on_loop_shutdown(
    key: _PoolKey, pool: AsyncConnectionPool
) -> AsyncGenerator[None, None]:
    """Own ``pool`` for its loop's lifetime; closing the generator closes the pool.

    Evict before closing: psycopg marks a pool closed *before* it awaits its
    workers, so a close that raises still leaves an unusable pool, and caching
    it would poison every later ``get_pool`` on this loop.
    """
    try:
        yield
    finally:
        _forget(key, pool)
        _, loop = key
        if loop.is_closed():
            _abandon(pool)
        else:
            await _close_pool(pool)


def _idle_connections(pool: AsyncConnectionPool) -> list:
    # psycopg_pool exposes no public view of its idle deque; see _close_pool.
    return list(getattr(pool, "_pool", ()))


async def _close_pool(pool: AsyncConnectionPool) -> None:
    """Close ``pool`` and make sure its idle connections really are closed.

    ``asyncio.run``/``Runner`` cancel every task before shutting down async
    generators, so by the time a loop's shutdown reaches us the pool's workers
    are already cancelled. ``pool.close()`` then re-raises that cancellation
    from gathering them — *after* detaching the idle connections and *before*
    closing them — which left every connection open until garbage collection.
    """
    idle = _idle_connections(pool)
    try:
        await pool.close()
    except BaseException as exc:
        # Only a close that raised skipped its own connection cleanup; on success
        # the snapshot may include connections a client has since checked out.
        for conn in idle:
            with contextlib.suppress(Exception):
                await conn.close()
        current = asyncio.current_task()
        stray_cancel = isinstance(exc, asyncio.CancelledError) and not (
            current is not None and current.cancelling()
        )
        if not stray_cancel:
            raise


def _abandon(pool: AsyncConnectionPool) -> None:
    """Release a pool whose loop closed without running its shutdown hook.

    Nothing can await on a closed loop, so drop the idle sockets synchronously
    instead of leaving them to the garbage collector. Connections checked out
    when the loop died are unreachable either way and close when collected.
    """
    for conn in _idle_connections(pool):
        with contextlib.suppress(Exception):
            conn.pgconn.finish()
    logger.warning("Released managed-DB pool %r left behind by a closed event loop", pool.name)


def release_pools_of_finished_loops() -> None:
    """Drop cached pools whose owning loop has closed.

    Normally the loop's own shutdown already closed them. This is the fallback
    for loops closed without ``shutdown_asyncgens``, and keeps them from holding
    ``_MAX_POOLS`` slots forever.
    """
    with _state_lock:
        # Claim under the lock so two threads never drive the same generator.
        dead = [_pools.pop(key) for key, entry in list(_pools.items()) if entry.loop.is_closed()]
    for entry in dead:
        # Drive the generator's finally by hand; with the loop closed it never awaits.
        closer = entry.lifetime.aclose()
        try:
            closer.send(None)
        except StopIteration:
            pass
        else:
            closer.close()


async def close_all_pools() -> None:
    """Close the current loop's pools and release any left by closed loops.

    Pools owned by *other live* loops are left alone: closing them from here is
    exactly the cross-loop operation this module exists to avoid, and their own
    loop closes them at shutdown. Used in tests and on shutdown.
    """
    loop = asyncio.get_running_loop()
    release_pools_of_finished_loops()
    with _state_lock:
        mine = [entry for (_, owner), entry in _pools.items() if owner is loop]

    # Isolate per-pool failures, or the first raising close would strand every
    # pool behind it — already evicted, so unreachable and leaked for the process.
    results = await asyncio.gather(
        *(entry.lifetime.aclose() for entry in mine), return_exceptions=True
    )
    cancelled = None
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            cancelled = result
        elif isinstance(result, BaseException):
            logger.warning("Failed to close managed-DB pool: %s", result)
    if cancelled is not None:
        # Never swallow cancellation: the caller's task is being torn down.
        raise cancelled
