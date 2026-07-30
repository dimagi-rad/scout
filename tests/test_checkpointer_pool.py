"""Pool-lifecycle tests for the LangGraph checkpointer singleton (arch #255, 08#1).

The module-global AsyncConnectionPool + AsyncPostgresSaver had three defects:
an unsynchronized init race (a second cold start closed the first's half-open
pool), force_new closing the shared pool out from under concurrent streams, and
no borrow-time health check. These tests lock in the fixed behavior with the
pool/saver fully mocked so no real Postgres is needed.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import apps.chat.checkpointer as ckpt


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """Each test starts from a clean singleton and restores it afterwards."""
    ckpt._checkpointer = None
    ckpt._pool = None
    ckpt._init_lock = asyncio.Lock()
    yield
    ckpt._checkpointer = None
    ckpt._pool = None


def _make_pool():
    pool = MagicMock(name="AsyncConnectionPool")
    pool.closed = False
    pool.open = AsyncMock(return_value=None)
    pool.close = AsyncMock(return_value=None)
    return pool


@pytest.mark.asyncio
async def test_concurrent_cold_starts_build_a_single_pool():
    """Two concurrent ensure_checkpointer() calls must init exactly one pool —
    the init lock serializes them instead of racing two half-open pools."""
    pools = []

    def _pool_factory(*args, **kwargs):
        p = _make_pool()
        pools.append(p)
        return p

    saver = MagicMock()
    saver.setup = AsyncMock(return_value=None)

    with (
        patch.object(ckpt, "AsyncConnectionPool", side_effect=_pool_factory) as pool_cls,
        patch.object(ckpt, "AsyncPostgresSaver", return_value=saver),
        patch.object(ckpt, "get_database_url", return_value="postgresql://x/y"),
    ):
        pool_cls.check_connection = MagicMock()
        results = await asyncio.gather(
            ckpt.ensure_checkpointer(),
            ckpt.ensure_checkpointer(),
        )

    assert len(pools) == 1, "concurrent cold starts must not each build a pool"
    assert results[0] is results[1] is saver


@pytest.mark.asyncio
async def test_force_new_reuses_open_pool_and_does_not_close_it():
    """force_new must rebuild the saver over the SAME open pool — never close a
    pool other in-flight streams are borrowing from for checkpoint writes."""
    existing_pool = _make_pool()
    ckpt._pool = existing_pool
    ckpt._checkpointer = MagicMock(name="old_saver")

    new_saver = MagicMock(name="new_saver")
    new_saver.setup = AsyncMock(return_value=None)

    with (
        patch.object(ckpt, "AsyncConnectionPool") as pool_cls,
        patch.object(ckpt, "AsyncPostgresSaver", return_value=new_saver),
        patch.object(ckpt, "get_database_url", return_value="postgresql://x/y"),
    ):
        pool_cls.check_connection = MagicMock()
        result = await ckpt.ensure_checkpointer(force_new=True)

    existing_pool.close.assert_not_awaited()
    pool_cls.assert_not_called()  # reused the open pool, no new one built
    assert ckpt._pool is existing_pool
    assert result is new_saver


@pytest.mark.asyncio
async def test_pool_created_with_borrow_time_health_check():
    """A freshly built pool must pass check= so dead pooled connections are
    recycled on checkout rather than handed out mid-write."""
    saver = MagicMock()
    saver.setup = AsyncMock(return_value=None)

    with (
        patch.object(ckpt, "AsyncConnectionPool", return_value=_make_pool()) as pool_cls,
        patch.object(ckpt, "AsyncPostgresSaver", return_value=saver),
        patch.object(ckpt, "get_database_url", return_value="postgresql://x/y"),
    ):
        pool_cls.check_connection = MagicMock(name="check_connection")
        await ckpt.ensure_checkpointer()

    _, kwargs = pool_cls.call_args
    assert kwargs.get("check") is pool_cls.check_connection


@pytest.mark.asyncio
async def test_closed_pool_is_rebuilt():
    """If the existing pool has been closed, ensure_checkpointer builds a new one
    rather than reusing the dead pool."""
    dead_pool = _make_pool()
    dead_pool.closed = True
    ckpt._pool = dead_pool
    ckpt._checkpointer = None

    fresh_pool = _make_pool()
    saver = MagicMock()
    saver.setup = AsyncMock(return_value=None)

    with (
        patch.object(ckpt, "AsyncConnectionPool", return_value=fresh_pool) as pool_cls,
        patch.object(ckpt, "AsyncPostgresSaver", return_value=saver),
        patch.object(ckpt, "get_database_url", return_value="postgresql://x/y"),
    ):
        pool_cls.check_connection = MagicMock()
        await ckpt.ensure_checkpointer()

    pool_cls.assert_called_once()
    assert ckpt._pool is fresh_pool
