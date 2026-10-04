"""Lazy singleton for the LangGraph async PostgreSQL checkpointer and its conninfo."""

import asyncio
import logging
import os

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from psycopg_pool import PoolTimeout

from apps.common.capacity import CapacityResource
from apps.common.capacity_pool import CapacityTaggingPool
from apps.common.db_urls import enforce_db_tls_conninfo

logger = logging.getLogger(__name__)

_checkpointer = None
_pool = None
# Serialize init so concurrent cold starts don't race the half-open pool (arch #255 08#1).
_init_lock = asyncio.Lock()


class CheckpointerPoolExhausted(PoolTimeout):
    """Every checkpointer connection stayed checked out past the pool timeout."""

    capacity_resource = CapacityResource.CHECKPOINTER_POOL


class CheckpointerPool(CapacityTaggingPool):
    """Tags a full pool as capacity so ``apps.common.capacity`` answers "busy"."""

    exhausted_error = CheckpointerPoolExhausted


def get_database_url() -> str:
    """Resolve the platform Postgres conninfo: ``DATABASE_URL``, else Django's default DB."""
    # DATABASE_URL first so query-string options (e.g. connect_timeout) survive; the
    # DATABASES dict below would drop them.
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return enforce_db_tls_conninfo(database_url)

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
    return enforce_db_tls_conninfo(
        make_conninfo(**{key: str(value) for key, value in params.items() if value})
    )


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


def thread_has_checkpoint(thread_id) -> bool:
    """True when the checkpointer holds conversation state under ``thread_id``.

    Deleting a Thread keeps its checkpoints (#265), so a Thread row must never be
    created for an id that has some: its new owner would resume the old conversation.
    Reads Django's connection because the saver's tables live in the same platform
    database (``get_database_url``); no table yet means no state yet.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('checkpoints') IS NOT NULL")
        if not cursor.fetchone()[0]:
            return False
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM checkpoints WHERE thread_id = %s)", [str(thread_id)]
        )
        return cursor.fetchone()[0]


async def athread_has_checkpoint(thread_id) -> bool:
    # Django has no async raw-SQL cursor.
    return await sync_to_async(thread_has_checkpoint)(thread_id)


def threads_with_checkpoints(thread_ids) -> set[str]:
    """The ids among ``thread_ids`` that hold checkpointer state, in one query."""
    ids = [str(thread_id) for thread_id in thread_ids]
    if not ids:
        return set()
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('checkpoints') IS NOT NULL")
        if not cursor.fetchone()[0]:
            return set()
        cursor.execute(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id = ANY(%s)", [ids]
        )
        return {row[0] for row in cursor.fetchall()}


async def athreads_with_checkpoints(thread_ids) -> set[str]:
    return await sync_to_async(threads_with_checkpoints)(list(thread_ids))
