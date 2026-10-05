"""Tests for the agent graph builder."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apps.agents.graph.prompt_context import _build_system_prompt, _fetch_semantic_model_context
from apps.agents.graph.tool_binding import (
    INJECTED_TOOL_PARAMS,
    MCP_TOOL_NAMES,
    _build_tools,
    _llm_tool_schemas,
)
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema


class TestMcpToolNames:
    """Verify MCP_TOOL_NAMES contains all tools that need workspace_id injection."""

    def test_get_schema_status_in_mcp_tool_names(self):

        assert "get_schema_status" in MCP_TOOL_NAMES

    def test_existing_tools_still_present(self):

        assert "list_tables" in MCP_TOOL_NAMES
        assert "describe_table" in MCP_TOOL_NAMES
        assert "query" in MCP_TOOL_NAMES
        assert "semantic_query" in MCP_TOOL_NAMES
        assert "semantic_catalog" in MCP_TOOL_NAMES
        assert "get_metadata" in MCP_TOOL_NAMES
        assert "run_materialization" in MCP_TOOL_NAMES
        assert "list_workspaces" in MCP_TOOL_NAMES
        assert "list_datasets" in MCP_TOOL_NAMES


class TestAgentToolSurface:
    def test_parent_graph_exposes_artifact_manager_not_primitives(self):

        workspace = SimpleNamespace(id="ws-1", system_prompt="")
        tools = _build_tools(workspace, None, [], write_capable=True)
        tool_names = {t.name for t in tools}

        assert "artifact_manager" in tool_names
        assert "create_artifact" not in tool_names
        assert "update_artifact" not in tool_names
        assert "artifact_write" not in tool_names
        assert "artifact_graph_overview" not in tool_names
        assert "get_artifact_semantic_queries" not in tool_names

    def test_artifact_manager_tool_call_id_hidden_from_llm_schema(self):

        workspace = SimpleNamespace(id="ws-1", system_prompt="")
        schemas = _llm_tool_schemas(
            _build_tools(workspace, None, [], write_capable=True),
            hidden_params=list(INJECTED_TOOL_PARAMS),
        )
        artifact_schema = next(
            item
            for item in schemas
            if isinstance(item, dict) and item["function"]["name"] == "artifact_manager"
        )
        props = artifact_schema["function"]["parameters"]["properties"]
        required = artifact_schema["function"]["parameters"]["required"]

        assert "task" in props
        assert "task" in required
        assert "intent" not in props
        assert "tool_call_id" not in props
        assert "subagent_event_queue" not in props
        assert "tool_call_id" not in required
        assert "subagent_event_queue" not in required


class TestHeadlessMode:
    """interactive=False swaps the interactive MCP run_materialization (fire-and-
    ack) for the headless blocking tool, and emits blocking prompt guidance."""

    def _fake_mcp_tool(self, name):
        t = MagicMock()
        t.name = name
        return t

    def test_build_tools_headless_swaps_in_blocking_materialization(self):

        mcp_tools = [
            self._fake_mcp_tool("semantic_query"),
            self._fake_mcp_tool("run_materialization"),
        ]
        workspace = SimpleNamespace(id="ws-1", system_prompt="")

        headless = _build_tools(
            workspace, None, mcp_tools, interactive=False, job_id=7, write_capable=True
        )
        rm = [t for t in headless if t.name == "run_materialization"]
        assert len(rm) == 1, "exactly one run_materialization tool"
        # The headless tool replaces the MCP one (different object identity).
        assert rm[0] not in mcp_tools
        assert callable(getattr(rm[0], "coroutine", None))  # async StructuredTool

    def test_build_tools_interactive_keeps_mcp_materialization(self):

        mcp_rm = self._fake_mcp_tool("run_materialization")
        mcp_tools = [self._fake_mcp_tool("semantic_query"), mcp_rm]
        workspace = SimpleNamespace(id="ws-1", system_prompt="")

        interactive = _build_tools(workspace, None, mcp_tools, interactive=True, write_capable=True)
        rm = [t for t in interactive if t.name == "run_materialization"]
        assert rm == [mcp_rm], "interactive path keeps the original MCP tool untouched"

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_headless_no_data_prompt_is_blocking_not_resume(self, workspace):

        interactive = await _fetch_semantic_model_context(
            workspace, interactive=True, write_capable=True
        )
        headless = await _fetch_semantic_model_context(
            workspace, interactive=False, write_capable=True
        )

        assert "run_materialization" in headless
        # Interactive tells the agent to end its turn and wait for an async resume.
        assert "end your turn" in interactive.lower()
        # Headless must NOT — it has no resume path; it blocks and continues.
        assert "end your turn" not in headless.lower()
        assert "block" in headless.lower()

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_headless_in_progress_prompt_does_not_trigger_second_materialization(
        self, workspace, tenant
    ):
        """When a materialization is already in progress, the headless prompt
        must NOT tell the agent "no data loaded → call run_materialization" (that
        starts a 2nd parallel run). It gets distinct in-progress guidance."""
        schema = await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="t_inprog", state=SchemaState.PROVISIONING
        )
        await MaterializationRun.objects.acreate(
            tenant_schema=schema, pipeline="commcare_sync", state="loading"
        )
        msg = await _fetch_semantic_model_context(workspace, interactive=False, write_capable=True)

        assert "run_materialization" in msg  # still the only path to data, headless
        assert "no data has been loaded" not in msg.lower()  # would imply "start one"
        assert "end your turn" not in msg.lower()  # no async resume in headless
        assert "in progress" in msg.lower()  # acknowledges the in-flight load


class TestSystemPrompt:
    """Verify the system prompt includes data availability instructions."""

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_data_availability_section_present(self, workspace, user, tenant):

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(workspace, user, write_capable=True))

        assert "Data Availability" in prompt
        # Schema context is now pre-fetched; no instruction to call get_schema_status
        assert "get_schema_status" not in prompt
        # When no schema exists, agent is told to call run_materialization
        assert "run_materialization" in prompt
        assert "list_workspaces" in prompt
        assert "list_datasets" in prompt
        # Runtime workspace/provider details are tool-discovered, not preloaded.
        assert tenant.canonical_name not in prompt
        assert tenant.external_id not in prompt
        assert "Pipeline:" not in prompt

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_data_availability_covers_not_loaded_case(self, workspace, user):

        # _build_system_prompt returns a (stable, volatile) split (arch #254).
        prompt = "\n".join(await _build_system_prompt(workspace, user, write_capable=True))

        # Agent must know to run materialization when no data exists
        assert "No data has been loaded yet" in prompt or "loading" in prompt.lower()
