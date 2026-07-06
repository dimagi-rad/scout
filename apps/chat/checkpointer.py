"""Lazy singleton for the LangGraph async PostgreSQL checkpointer."""

import logging

from django.conf import settings
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from apps.agents.memory.checkpointer import get_database_url

logger = logging.getLogger(__name__)

_checkpointer = None
_pool = None


def _get_pool_config() -> tuple[int, int, int]:
    min_size = settings.LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE
    max_size = settings.LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE
    open_timeout = settings.LANGGRAPH_CHECKPOINT_POOL_OPEN_TIMEOUT_S

    if min_size < 0:
        raise ValueError("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE must be >= 0")
    if max_size < 1:
        raise ValueError("LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE must be >= 1")
    if min_size > max_size:
        raise ValueError("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE must be <= LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE")
    if open_timeout < 1:
        raise ValueError("LANGGRAPH_CHECKPOINT_POOL_OPEN_TIMEOUT_S must be >= 1")

    return min_size, max_size, open_timeout


async def ensure_checkpointer(*, force_new: bool = False):
    global _checkpointer, _pool
    if _checkpointer is not None and not force_new:
        return _checkpointer

    try:
        database_url = get_database_url()
        min_size, max_size, open_timeout = _get_pool_config()

        if _pool is not None:
            await _pool.close()

        _pool = AsyncConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
            },
        )
        await _pool.open(wait=True, timeout=open_timeout)

        _checkpointer = AsyncPostgresSaver(_pool)
        await _checkpointer.setup()
        logger.info(
            "PostgreSQL checkpointer initialized (pool min=%s max=%s)",
            min_size,
            max_size,
        )
    except Exception as e:
        if settings.DEBUG:
            logger.warning(
                "PostgreSQL checkpointer unavailable, using MemorySaver (DEBUG only): %s", e
            )
            _checkpointer = MemorySaver()
        else:
            logger.error(
                "PostgreSQL checkpointer failed in production — conversation history unavailable: %s",
                e,
                exc_info=True,
            )
            raise

    return _checkpointer
