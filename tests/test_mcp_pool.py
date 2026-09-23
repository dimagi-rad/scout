"""Managed-DB connection pooling for the MCP server (arch #253, finding 10#1).

Previously every MCP query/describe/list opened a fresh psycopg TLS connection.
We now reuse a shared ``AsyncConnectionPool``, keyed by the base DB identity
(host/port/dbname/user) rather than the per-schema search_path — so two
different schemas in the same managed DB share one pool, and a second query does
not pay another TLS handshake.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.services import pool as pool_mod


@pytest.fixture(autouse=True)
def _clear_pools():
    pool_mod._pools.clear()
    yield
    pool_mod._pools.clear()


def _fake_pool():
    """A mock that looks live: the cache now drops pools that report themselves closed."""
    return MagicMock(open=AsyncMock(), close=AsyncMock(), closed=False)


def _base_params(schema):
    return {
        "host": "db.example.com",
        "port": 5432,
        "dbname": "scout",
        "user": "scout_app",
        "password": "pw",
        "sslmode": "require",
        # per-schema option that must NOT affect the pool key
        "options": f"-c search_path={schema},public -c statement_timeout=30000",
    }


@pytest.mark.asyncio
async def test_get_pool_reuses_pool_for_same_base_db():
    """Two contexts on different schemas of the same managed DB share one pool —
    proving connections are reused, not reopened per schema."""
    fake_pool = _fake_pool()

    with patch.object(pool_mod, "AsyncConnectionPool", return_value=fake_pool) as PoolCls:
        p1 = await pool_mod.get_pool(_base_params("t_alpha"))
        p2 = await pool_mod.get_pool(_base_params("t_beta"))

    assert p1 is p2
    # Pool constructed exactly once despite two different search_paths.
    assert PoolCls.call_count == 1
    fake_pool.open.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_pool_separate_pools_for_different_dbs():
    """Different managed databases get distinct pools."""
    fake_a = _fake_pool()
    fake_b = _fake_pool()

    with patch.object(pool_mod, "AsyncConnectionPool", side_effect=[fake_a, fake_b]) as PoolCls:
        a = _base_params("t_a")
        b = _base_params("t_b")
        b["dbname"] = "other_db"
        pa = await pool_mod.get_pool(a)
        pb = await pool_mod.get_pool(b)

    assert pa is not pb
    assert PoolCls.call_count == 2


@pytest.mark.asyncio
async def test_base_conninfo_excludes_per_schema_options():
    """The conninfo passed to the pool carries the base DB identity but not the
    per-schema search_path options."""
    fake_pool = _fake_pool()
    with patch.object(pool_mod, "AsyncConnectionPool", return_value=fake_pool) as PoolCls:
        await pool_mod.get_pool(_base_params("t_x"))

    conninfo = PoolCls.call_args.kwargs["conninfo"]
    assert "dbname='scout'" in conninfo
    assert "host='db.example.com'" in conninfo
    assert "search_path" not in conninfo


@pytest.mark.asyncio
async def test_failed_close_does_not_strand_the_pool_in_the_cache():
    """A close that raises must still evict the pool.

    psycopg sets ``_closed`` before it awaits its workers, so a pool whose close
    raised is already unusable. Leaving it cached made every later ``get_pool``
    return a dead pool and raise ``PoolClosed`` for the life of the process.
    """
    dying = _fake_pool()
    dying.close = AsyncMock(side_effect=RuntimeError("boom"))

    with patch.object(pool_mod, "AsyncConnectionPool", return_value=dying):
        await pool_mod.get_pool(_base_params("t_alpha"))

    await pool_mod.close_all_pools()

    assert pool_mod._pools == {}

    revived = _fake_pool()
    with patch.object(pool_mod, "AsyncConnectionPool", return_value=revived) as PoolCls:
        assert await pool_mod.get_pool(_base_params("t_alpha")) is revived
    assert PoolCls.call_count == 1


@pytest.mark.asyncio
async def test_get_pool_replaces_a_pool_that_reports_itself_closed():
    """A cached-but-closed pool is rebuilt rather than handed out."""
    stale = _fake_pool()
    with patch.object(pool_mod, "AsyncConnectionPool", return_value=stale):
        await pool_mod.get_pool(_base_params("t_alpha"))

    stale.closed = True

    fresh = _fake_pool()
    with patch.object(pool_mod, "AsyncConnectionPool", return_value=fresh) as PoolCls:
        assert await pool_mod.get_pool(_base_params("t_alpha")) is fresh
    assert PoolCls.call_count == 1
    stale.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_all_pools_propagates_a_real_cancellation():
    """Swallowing the pool's own worker-cancellation must not swallow ours."""
    closing = asyncio.Event()

    async def slow_close():
        closing.set()
        await asyncio.sleep(10)

    pool = _fake_pool()
    pool.close = AsyncMock(side_effect=slow_close)
    with patch.object(pool_mod, "AsyncConnectionPool", return_value=pool):
        await pool_mod.get_pool(_base_params("t_alpha"))

    task = asyncio.create_task(pool_mod.close_all_pools())
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool_mod._pools == {}


@pytest.mark.asyncio
async def test_a_pool_whose_workers_were_cancelled_still_closes_quietly():
    """Loop teardown cancels the pool's workers before our shutdown hook runs,
    so ``pool.close()`` raises CancelledError that is not ours to propagate."""
    pool = _fake_pool()
    pool.close = AsyncMock(side_effect=asyncio.CancelledError())
    with patch.object(pool_mod, "AsyncConnectionPool", return_value=pool):
        await pool_mod.get_pool(_base_params("t_alpha"))

    await pool_mod.close_all_pools()

    pool.close.assert_awaited_once()
    assert pool_mod._pools == {}


@pytest.mark.asyncio
async def test_close_all_pools_closes_every_pool_even_when_one_raises():
    """One failing close must not strand the pools queued behind it.

    They are already evicted, so a close that aborts early leaves them
    unreachable and leaks their connections and worker tasks for the process.
    """
    bad = _fake_pool()
    bad.close = AsyncMock(side_effect=RuntimeError("boom"))
    good = _fake_pool()

    with patch.object(pool_mod, "AsyncConnectionPool", side_effect=[bad, good]):
        first = _base_params("t_a")
        second = _base_params("t_b")
        second["dbname"] = "other_db"
        await pool_mod.get_pool(first)
        await pool_mod.get_pool(second)

    await pool_mod.close_all_pools()

    bad.close.assert_awaited_once()
    good.close.assert_awaited_once()
    assert pool_mod._pools == {}
