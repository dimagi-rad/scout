"""Lazy singleton for the LangGraph async PostgreSQL checkpointer and its conninfo."""

import asyncio
import logging
import os

from django.conf import settings
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from apps.common.capacity import CapacityResource

logger = logging.getLogger(__name__)

_checkpointer = None
_pool = None
# Serialize init so concurrent cold starts don't race the half-open pool (arch #255 08#1).
_init_lock = asyncio.Lock()


class CheckpointerPoolExhausted(PoolTimeout):
    """Every checkpointer connection stayed checked out past the pool timeout."""

    capacity_resource = CapacityResource.CHECKPOINTER_POOL


class CheckpointerPool(AsyncConnectionPool):
    """Tags a full pool as capacity so ``apps.common.capacity`` answers "busy".

    Only a checkout timeout is tagged: ``open()`` also raises ``PoolTimeout`` when
    the database is down or refusing auth, which retrying would not fix.
    """

    async def getconn(self, timeout: float | None = None):  # noqa: ASYNC109 -- psycopg_pool signature
        try:
            return await super().getconn(timeout)
        except PoolTimeout as exc:
            raise CheckpointerPoolExhausted(str(exc)) from exc


def get_database_url() -> str:
    """Resolve the platform Postgres conninfo: ``DATABASE_URL``, else Django's default DB."""
    # DATABASE_URL first so query-string options (e.g. sslmode) survive; the
    # DATABASES dict below would drop them.
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    db_config = settings.DATABASES.get("default", {})
    engine = db_config.get("ENGINE", "")
    if "postgres" not in engine.lower():
        raise ValueError(f"Django default database is not PostgreSQL: {engine}")
    if not db_config.get("NAME"):
        raise ValueError("Django default database has no NAME")

    # Django stores unset keys as "", so drop them and let libpq apply its own
    # defaults (socket/localhost, 5432, OS user); make_conninfo also quotes values.
    params = {
        "dbname": db_config.get("NAME"),
        "host": db_config.get("HOST"),
        "port": db_config.get("PORT"),
        "user": db_config.get("USER"),
        "password": db_config.get("PASSWORD"),
    }
    return make_conninfo(**{key: str(value) for key, value in params.items() if value})


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
                _pool = CheckpointerPool(
                    conninfo=database_url,
                    min_size=min_size,
                    max_size=max_size,
                    open=False,
                    # Recycle a dead pooled connection on checkout, not mid-write (arch #255 08#1).
                    check=CheckpointerPool.check_connection,
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
