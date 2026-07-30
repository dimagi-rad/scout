"""Managed-DB connection pooling for the MCP server (arch #253, finding 10#1).

Previously every MCP query/describe/list opened a fresh psycopg TLS connection.
We now reuse a shared ``AsyncConnectionPool``, keyed by the base DB identity
(host/port/dbname/user) rather than the per-schema search_path — so two
different schemas in the same managed DB share one pool, and a second query does
not pay another TLS handshake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.services import pool as pool_mod


@pytest.fixture(autouse=True)
def _clear_pools():
    pool_mod._pools.clear()
    yield
    pool_mod._pools.clear()


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
    fake_pool = MagicMock()
    fake_pool.open = AsyncMock()

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
    fake_a = MagicMock(open=AsyncMock())
    fake_b = MagicMock(open=AsyncMock())

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
    fake_pool = MagicMock(open=AsyncMock())
    with patch.object(pool_mod, "AsyncConnectionPool", return_value=fake_pool) as PoolCls:
        await pool_mod.get_pool(_base_params("t_x"))

    conninfo = PoolCls.call_args.kwargs["conninfo"]
    assert "dbname='scout'" in conninfo
    assert "host='db.example.com'" in conninfo
    assert "search_path" not in conninfo
