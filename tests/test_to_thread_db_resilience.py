"""Dead-connection hygiene for sync ORM run on asyncio.to_thread pool threads
(arch #253, finding 08#0).

``build_view_schema`` (heavy sync ORM) and ``run_pipeline`` (the whole
ORM-writing materialization) run via ``asyncio.to_thread`` on default-executor
pool threads. Those threads have their own thread-local Django connection that
the worker task decorator's cleanup (which only reaches the async-ORM thread)
cannot reach — so a pool thread holding a connection that died since the last
run poisons view-schema rebuilds and refreshes. ``_to_thread_fresh_db`` runs
``close_old_connections`` on the SAME pool thread, immediately before the body,
so the body re-opens a fresh connection — without ever closing the connection a
synchronous test/request on the calling thread is using.
"""

from __future__ import annotations

import pytest

from apps.workspaces import tasks as workspace_tasks
from apps.workspaces.tasks import _to_thread_fresh_db


@pytest.mark.asyncio
async def test_to_thread_fresh_db_closes_connections_before_body(monkeypatch):
    """The wrapper closes stale/dead connections BEFORE running the body, on the
    pool thread, and forwards args/return value."""
    order: list[str] = []

    def _fake_close():
        order.append("close")

    def _body(a, b, *, kw):
        order.append("body")
        return (a, b, kw)

    monkeypatch.setattr(workspace_tasks, "close_old_connections", _fake_close)

    result = await _to_thread_fresh_db(_body, 1, 2, kw=3)

    assert result == (1, 2, 3)
    # close_old_connections must run first, then the body — never the reverse.
    assert order == ["close", "body"]


@pytest.mark.asyncio
async def test_to_thread_fresh_db_closes_even_if_body_raises(monkeypatch):
    """Cleanup runs before the body, so a body that fails still got a fresh
    connection (the failure is not caused by a stale one)."""
    order: list[str] = []
    monkeypatch.setattr(workspace_tasks, "close_old_connections", lambda: order.append("close"))

    def _body():
        order.append("body")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _to_thread_fresh_db(_body)

    assert order == ["close", "body"]
