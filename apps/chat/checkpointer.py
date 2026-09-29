"""Lazy singleton for the LangGraph async PostgreSQL checkpointer."""

import asyncio
import logging
import os

from django.conf import settings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

_checkpointer = None
_pool = None
# Serialize init so concurrent cold starts don't race the half-open pool (arch #255 08#1).
_init_lock = asyncio.Lock()


def get_database_url() -> str:
    """Resolve the platform Postgres URL: ``DATABASE_URL``, else Django's default DB."""
    # DATABASE_URL first so query-string options (e.g. sslmode) survive; the
    # DATABASES dict below would drop them.
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    db_config = settings.DATABASES.get("default", {})
    engine = db_config.get("ENGINE", "")
    if "postgres" not in engine.lower():
        raise ValueError(f"Django default database is not PostgreSQL: {engine}")

    host = db_config.get("HOST", "localhost")
    port = db_config.get("PORT", 5432)
    name = db_config.get("NAME")
    user = db_config.get("USER")
    password = db_config.get("PASSWORD", "")
    if not all([host, name, user]):
        raise ValueError("Incomplete Django database configuration")

    password_part = f":{password}" if password else ""
    return f"postgresql://{user}{password_part}@{host}:{port}/{name}"


def _pool_is_usable(pool) -> bool:
    """True if ``pool`` exists and has not been closed."""
    return pool is not None and not getattr(pool, "closed", False)


def _get_pool_config() -> tuple[int, int, int]:
    min_size = settings.LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE
    max_size = settings.LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE
    open_timeout = settings.LANGGRAPH_CHECKPOINT_POOL_OPEN_TIMEOUT_S

    if min_size < 0:
        raise ValueError("LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE must be >= 0")
    if max_size < 1:
        raise ValueError("LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE must be >= 1")
    if min_size > max_size:
        raise ValueError(
            "LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE must be <= LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE"
        )
    if open_timeout < 1:
        raise ValueError("LANGGRAPH_CHECKPOINT_POOL_OPEN_TIMEOUT_S must be >= 1")

    return min_size, max_size, open_timeout


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
            min_size, max_size, open_timeout = _get_pool_config()

            # force_new rebuilds only the stateless saver; it must NOT close a pool
            # other in-flight streams are still borrowing for writes (arch #255 08#1).
            if not _pool_is_usable(_pool):
                _pool = AsyncConnectionPool(
                    conninfo=database_url,
                    min_size=min_size,
                    max_size=max_size,
                    open=False,
                    # Recycle a dead pooled connection on checkout, not mid-write (arch #255 08#1).
                    check=AsyncConnectionPool.check_connection,
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
            # No MemorySaver fallback, even under DEBUG: a cached in-memory saver
            # silently drops every later conversation until restart (#266 07#8).
            logger.error(
                "PostgreSQL checkpointer failed — conversation history unavailable: %s",
                e,
                exc_info=True,
            )
            raise

    return _checkpointer
