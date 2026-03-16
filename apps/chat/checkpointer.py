"""Lazy singleton for the LangGraph async PostgreSQL checkpointer."""

import logging

from apps.agents.memory.checkpointer import get_database_url

logger = logging.getLogger(__name__)

_checkpointer = None
_pool = None


async def ensure_checkpointer(*, force_new: bool = False):
    global _checkpointer, _pool
    if _checkpointer is not None and not force_new:
        return _checkpointer

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        database_url = get_database_url()

        if _pool is not None:
            await _pool.close()

        _pool = AsyncConnectionPool(
            conninfo=database_url,
            max_size=20,
            open=False,
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
        from django.conf import settings

        if settings.DEBUG:
            from langgraph.checkpoint.memory import MemorySaver

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
