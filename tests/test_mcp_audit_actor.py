"""Tests for actor attribution in the MCP audit trail (arch #257, finding 08#8).

The MCP-side ``tool_context`` audit covered every tool but ``context_id`` was the
workspace id only — no user or thread — so the trail could not answer "who ran
this tool, in which conversation". The agent graph already injects ``user_id``
and ``thread_id`` into every MCP tool call, so threading those through to
``tool_context`` gives the audit record a real actor.
"""

from __future__ import annotations

import logging

import pytest

from mcp_server.envelope import tool_context
from mcp_server.server import query


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tool_context_logs_actor_when_provided(caplog):
    """tool_context records user_id and thread_id when threaded through."""
    with caplog.at_level(logging.INFO, logger="mcp_server.audit"):
        async with tool_context(
            "query", "ws-1", user_id="user-42", thread_id="thread-9", sql="SELECT 1"
        ) as tc:
            tc["result"] = {"success": True}

    records = [r for r in caplog.records if r.name == "mcp_server.audit"]
    assert records, "expected an MCP audit record"
    msg = records[0].getMessage()
    assert "user_id='user-42'" in msg, f"audit line missing actor user: {msg!r}"
    assert "thread_id='thread-9'" in msg, f"audit line missing actor thread: {msg!r}"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tool_context_omits_empty_actor(caplog):
    """Empty actor fields are not noise-logged (context-free / operator tools)."""
    with caplog.at_level(logging.INFO, logger="mcp_server.audit"):
        async with tool_context("list_pipelines", "") as tc:
            tc["result"] = {"success": True}

    records = [r for r in caplog.records if r.name == "mcp_server.audit"]
    assert records
    msg = records[0].getMessage()
    assert "user_id=" not in msg
    assert "thread_id=" not in msg


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_query_tool_threads_actor_into_audit(caplog):
    """The real ``query`` MCP tool must thread the injected user/thread into the
    audit record so the trail has an actor, not just a workspace id."""
    # Empty workspace_id short-circuits with a VALIDATION_ERROR before any DB
    # work, but the audit line is still emitted on context exit — which is where
    # the actor must appear.
    with caplog.at_level(logging.INFO, logger="mcp_server.audit"):
        await query(
            sql="SELECT 1",
            workspace_id="",
            user_id="actor-user-1",
            thread_id="actor-thread-1",
        )

    records = [r for r in caplog.records if r.name == "mcp_server.audit"]
    assert records, "expected an MCP audit record from query"
    msg = records[0].getMessage()
    assert "user_id='actor-user-1'" in msg, f"query did not thread actor user: {msg!r}"
    assert "thread_id='actor-thread-1'" in msg, f"query did not thread actor thread: {msg!r}"
