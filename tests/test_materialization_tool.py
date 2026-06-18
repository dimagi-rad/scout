"""Tests for the headless blocking materialization agent-side tool."""

import pytest

from apps.agents.tools.materialization_tool import create_materialization_tool


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_materialization_tool_blocks_and_reports_completion(
    workspace, user, monkeypatch
):
    """The headless tool calls materialize_workspace_core, blocks on it, and
    returns a completion summary the agent can act on in the same run — unlike
    the interactive fire-and-ack MCP tool."""

    async def _fake_core(workspace_id, user_id="", job_id=None):
        assert workspace_id == str(workspace.id)
        assert job_id == 99
        return {
            "all_succeeded": True,
            "tenants": [{"tenant": "t1", "success": True}],
            "view_schema": None,
        }

    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_blocking", _fake_core)

    tool = create_materialization_tool(workspace, user, job_id=99)
    assert tool.name == "run_materialization"

    result = await tool.ainvoke({})
    assert result["status"] == "completed"
    assert result["tenants_loaded"] == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_materialization_tool_reports_failure(workspace, user, monkeypatch):
    """When no tenant loads, the tool reports failed so the agent can stop
    rather than query an empty schema."""

    async def _fake_core(workspace_id, user_id="", job_id=None):
        return {
            "all_succeeded": False,
            "tenants": [{"tenant": "t1", "success": False, "error": "boom"}],
            "view_schema": None,
        }

    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_blocking", _fake_core)

    tool = create_materialization_tool(workspace, user)
    result = await tool.ainvoke({})
    assert result["status"] == "failed"
    assert result["tenants_loaded"] == 0
