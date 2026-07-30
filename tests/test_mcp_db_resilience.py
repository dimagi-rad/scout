"""MCP server DB-connection resilience (arch #253, finding 08#0).

The MCP server is a long-lived Django-ORM process with no HTTP request cycle,
exactly like the procrastinate worker. A DB connection that dies underneath it
(RDS restart/upgrade, idle TCP timeout) is reused — closed — forever, so after
the next RDS maintenance window every ORM-touching MCP tool call fails until the
process restarts (the June 2026 22h-outage class, fixed only for the worker).

``tool_context`` wraps every MCP tool call, so it is the per-call hook where we
mirror the worker's ``close_old_connections`` hygiene: close a dead/stale
connection before the body so it re-opens, and close after so nothing leaks
between calls.
"""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import OperationalError, connections

from mcp_server.envelope import tool_context

User = get_user_model()


def _kill_default_connection():
    """Close the underlying psycopg connection behind Django's back — the exact
    state the MCP process was stuck in after an RDS maintenance window."""
    conn = connections["default"]
    conn.ensure_connection()
    conn.connection.close()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tool_context_recovers_from_dead_connection():
    """An ORM call inside tool_context succeeds even when the connection died
    since the previous tool call — tool_context closes the dead connection on
    entry so the ORM re-opens it."""
    await sync_to_async(_kill_default_connection)()

    async with tool_context("list_tables", "ws-1") as tc:
        # Would raise OperationalError without the entry-time cleanup.
        count = await User.objects.acount()
        tc["result"] = {"success": True}

    assert count == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_orm_call_fails_on_dead_connection_without_recovery():
    """Control: documents the failure mode tool_context's cleanup exists to fix."""
    await sync_to_async(_kill_default_connection)()
    with pytest.raises(OperationalError):
        await User.objects.acount()
    await sync_to_async(lambda: connections["default"].close())()
