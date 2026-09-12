"""Tests for the headless blocking materialization agent-side tool."""

import pytest

from apps.agents.tools.materialization_tool import create_materialization_tool
from apps.common.error_codes import ErrorCode


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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_tool_names_a_tenant_the_run_could_not_load(workspace, user, monkeypatch):
    """#364: this tool is the consumer of ``all_succeeded``. A workspace tenant
    the run did not cover must reach the agent by name, with the run's own
    remediation copy — "partial" alone leaves it nothing to disclose."""

    async def _fake_core(workspace_id, user_id="", job_id=None):
        return {
            "all_succeeded": False,
            "tenants": [
                {"tenant": "reachable", "success": True},
                {
                    "tenant": "not-mine",
                    "success": False,
                    "error": "no live commcare membership for the acting user",
                    "error_code": ErrorCode.WORKSPACE_TENANT_UNREACHABLE,
                },
            ],
            "view_schema": {"ok": True, "error": None},
            "guidance": ["not-mine: connect that account"],
        }

    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_blocking", _fake_core)

    tool = create_materialization_tool(workspace, user)
    result = await tool.ainvoke({})

    assert result["status"] == "partial"
    assert result["tenants_loaded"] == 1
    assert result["tenants_not_loaded"] == ["not-mine"]
    assert "not-mine" in result["message"]
    assert "connect that account" in result["message"]
    assert "older data may still be included" in result["message"]
    assert "NOT in the results" not in result["message"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_tool_reports_an_unqueryable_workspace(workspace, user, monkeypatch):
    """Every tenant can load and still leave nothing queryable, so this branch is
    reachable with no tenant to name — it must not emit "others did not: ." and
    must say what actually went wrong."""

    async def _fake_core(workspace_id, user_id="", job_id=None):
        return {
            "all_succeeded": True,
            "tenants": [{"tenant": "t1", "success": True}, {"tenant": "t2", "success": True}],
            "view_schema": {"ok": False, "error": "canonical name collision on 'x'"},
            "cube_schema": None,
            "guidance": [],
        }

    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_blocking", _fake_core)

    tool = create_materialization_tool(workspace, user)
    result = await tool.ainvoke({})

    assert result["status"] == "failed"
    assert result["tenants_not_loaded"] == []
    assert "others did not: ." not in result["message"]
    assert "canonical name collision" in result["message"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_tool_does_not_claim_data_loaded_when_nothing_loaded(
    workspace, user, monkeypatch
):
    """build_view_schema runs for every multi-tenant workspace with no success
    guard, and raises when a tenant has no ACTIVE schema — so "nothing loaded"
    and "view build failed" co-occur whenever credentials expire. Reporting that
    as "the tenant data loaded ... do NOT retry" inverts both facts and
    contradicts the run's own reconnect guidance."""

    async def _fake_core(workspace_id, user_id="", job_id=None):
        return {
            "all_succeeded": False,
            "tenants": [
                {"tenant": "t1", "success": False, "error": "HTTP 401", "error_code": "x"},
                {"tenant": "t2", "success": False, "error": "HTTP 401", "error_code": "x"},
            ],
            "view_schema": {
                "ok": False,
                "error": "Tenant 't1' has no active schema. Run a data refresh for this tenant.",
            },
            "cube_schema": None,
            "guidance": ["t1, t2: expired or revoked sign-in — reconnect the affected account"],
        }

    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_blocking", _fake_core)

    tool = create_materialization_tool(workspace, user)
    result = await tool.ainvoke({})

    assert result["status"] == "failed"
    assert result["tenants_loaded"] == 0
    assert result["tenants_not_loaded"] == ["t1", "t2"]
    assert "loaded, but" not in result["message"]
    assert "Do NOT retry" not in result["message"]
    # "Run a data refresh" cannot succeed before reconnecting, so the raw view
    # error must not be relayed on top of the reconnect guidance (#412).
    assert "Run a data refresh" not in result["message"]
    assert "reconnect the affected account" in result["message"]
    assert "t1, t2" in result["message"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_headless_tool_partial_load_with_failed_view_stays_partial(
    workspace, user, monkeypatch
):
    """A failed tenant leaves no ACTIVE schema, so a partial load takes the view
    build down with it. That is not a system-side build defect — the fix is the
    failed source — so it must not inherit "do NOT retry", but the agent still
    has to know the combined surface is gone."""

    async def _fake_core(workspace_id, user_id="", job_id=None):
        return {
            "all_succeeded": False,
            "tenants": [
                {"tenant": "t1", "success": True},
                {"tenant": "t2", "success": False, "error": "HTTP 500"},
            ],
            "view_schema": {
                "ok": False,
                "error": "Tenant 't2' has no active schema. Run a data refresh for this tenant.",
            },
            "cube_schema": None,
            "guidance": [],
        }

    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_blocking", _fake_core)

    tool = create_materialization_tool(workspace, user)
    result = await tool.ainvoke({})

    assert result["status"] == "partial"
    assert result["tenants_not_loaded"] == ["t2"]
    assert "Do NOT retry" not in result["message"]
    assert "Run a data refresh" not in result["message"]
    assert "combined query layer" in result["message"]
    assert "Do not query" in result["message"]
    assert "Proceed with the available" not in result["message"]
    assert "query only the sources" not in result["message"]
    assert "t2" in result["message"]
