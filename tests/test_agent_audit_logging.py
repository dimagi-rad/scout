"""Tests for the agent tool-call audit trail (arch #257, finding 08#8).

The audit trail is the only user/thread-attributed record of agent tool calls.
Two defects made it useless in production:

1. ``scout.agent.audit`` logs at INFO, but production LOGGING set root WARNING
   with only django/apps/mcp_server loggers configured, so the line was
   suppressed entirely in production.
2. The Django-side audit line logged ``input_state.get("project_id", "")`` which
   is ALWAYS empty — the agent state carries ``workspace_id`` (the
   projects->workspaces rename), so the workspace attribution was blank.

These tests pin the corrected behavior: the production config routes the audit
logger, and the emitted line carries the real workspace id.
"""

from __future__ import annotations

import importlib
import logging

import pytest

import config.settings.production as prod
from apps.chat import stream


def test_production_logging_routes_agent_audit_logger():
    """Production LOGGING must configure ``scout.agent.audit`` at INFO so the
    agent tool-call audit trail is actually emitted (not swallowed by root
    WARNING)."""
    # Reload so we read the module's declared LOGGING regardless of which
    # settings module the test session booted under.
    importlib.reload(prod)
    loggers = prod.LOGGING["loggers"]

    assert "scout.agent.audit" in loggers, (
        "production LOGGING must declare the 'scout.agent.audit' logger; "
        "under root WARNING an unconfigured INFO logger is dropped"
    )
    cfg = loggers["scout.agent.audit"]
    assert cfg["level"] == "INFO"
    assert cfg["handlers"], "audit logger needs a handler to emit anywhere"


def test_production_logging_routes_mcp_audit_logger():
    """The MCP-side audit logger must likewise be routed in production."""
    importlib.reload(prod)
    loggers = prod.LOGGING["loggers"]
    assert "mcp_server.audit" in loggers or loggers.get("mcp_server", {}).get("level") == "INFO", (
        "mcp_server.audit must be emitted in production (its parent 'mcp_server' "
        "logger is INFO, but propagate must reach a handler)"
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stream_audit_line_logs_workspace_id_not_empty_project_id(caplog):
    """The Django-side audit line must record the real workspace id.

    Regression: the line logged ``input_state.get("project_id")`` which is always
    empty because the state carries ``workspace_id``. It must log the workspace id.
    """

    # Minimal fake agent that emits a single on_tool_end event.
    class _FakeOutput:
        content = "ok"
        tool_call_id = "toolu_test"

    class _FakeAgent:
        def astream_events(self, input_state, config, version):
            async def _gen():
                yield {
                    "event": "on_tool_end",
                    "run_id": "run-1",
                    "name": "query",
                    "data": {"output": _FakeOutput()},
                }

            return _gen()

    input_state = {
        "workspace_id": "ws-abc-123",
        "user_id": "user-9",
    }
    config = {"configurable": {"thread_id": "thread-7"}}

    with caplog.at_level(logging.INFO, logger="scout.agent.audit"):
        async for _ in stream.langgraph_to_ui_stream(_FakeAgent(), input_state, config):
            pass

    audit_records = [r for r in caplog.records if r.name == "scout.agent.audit"]
    assert audit_records, "expected an audit log record for the tool call"
    msg = audit_records[0].getMessage()
    assert "workspace_id=ws-abc-123" in msg, f"audit line missing workspace id: {msg!r}"
    assert "user_id=user-9" in msg
    assert "thread_id=thread-7" in msg
    # The always-empty project_id key must be gone.
    assert "project_id=" not in msg, f"audit line still logs project_id: {msg!r}"
