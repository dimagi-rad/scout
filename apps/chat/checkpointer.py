"""Lazy singleton for the LangGraph async PostgreSQL checkpointer."""

import asyncio
import logging

from django.conf import settings
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from apps.agents.memory.checkpointer import get_database_url

logger = logging.getLogger(__name__)

_checkpointer = None
_pool = None
# Serializes initialization so two concurrent cold-start requests can't interleave
# (the second used to close the first's half-open pool, failing its setup — arch
# #255, 08#1).
_init_lock = asyncio.Lock()


def _pool_is_usable(pool) -> bool:
    """True if ``pool`` exists and has not been closed."""
    return pool is not None and not getattr(pool, "closed", False)


async def ensure_checkpointer(*, force_new: bool = False):
    global _checkpointer, _pool

    if _checkpointer is not None and not force_new:
        return _checkpointer

    async with _init_lock:
        # Re-check under the lock: another coroutine may have finished the build
        # while we were waiting, in which case reuse it instead of rebuilding.
        if _checkpointer is not None and not force_new:
            return _checkpointer

        try:
            database_url = get_database_url()

            # Reuse the existing pool if it is still open. force_new rebuilds the
            # (cheap, stateless) saver but MUST NOT close a pool that other
            # in-flight chat streams are borrowing from for checkpoint writes —
            # closing it turned one request's transient error into a failure for
            # every concurrent conversation in this worker (arch #255, 08#1). The
            # borrow-time health check below recycles individually-dead
            # connections, so a full pool rebuild is rarely needed anyway.
            if not _pool_is_usable(_pool):
                _pool = AsyncConnectionPool(
                    conninfo=database_url,
                    max_size=20,
                    open=False,
                    # Health-check each connection on checkout so a dead pooled
                    # connection (after a DB blip) is recycled instead of handed
                    # out mid-write.
                    check=AsyncConnectionPool.check_connection,
                    kwargs={
                        "autocommit": True,
                        "prepare_threshold": 0,
                    },
                )
                await _pool.open(wait=True, timeout=10)

            _checkpointer = AsyncPostgresSaver(_pool)
            await _checkpointer.setup()
            logger.info("PostgreSQL checkpointer initialized")
        except Exception as e:
            if settings.DEBUG:
                logger.warning(
                    "PostgreSQL checkpointer unavailable, using MemorySaver (DEBUG only): %s", e
                )
                _checkpointer = MemorySaver()
            else:
                logger.error(
                    "PostgreSQL checkpointer failed in production — conversation history "
                    "unavailable: %s",
                    e,
                    exc_info=True,
                )
                raise

    return _checkpointer
