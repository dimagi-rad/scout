"""Tests for the agent graph builder."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class TestMcpToolNames:
    """Verify MCP_TOOL_NAMES contains all tools that need workspace_id injection."""

    def test_get_schema_status_in_mcp_tool_names(self):
        from apps.agents.graph.base import MCP_TOOL_NAMES

        assert "get_schema_status" in MCP_TOOL_NAMES

    def test_existing_tools_still_present(self):
        from apps.agents.graph.base import MCP_TOOL_NAMES

        assert "list_tables" in MCP_TOOL_NAMES
        assert "describe_table" in MCP_TOOL_NAMES
        assert "semantic_query" in MCP_TOOL_NAMES
        assert "semantic_catalog" in MCP_TOOL_NAMES
        assert "get_metadata" in MCP_TOOL_NAMES
        assert "run_materialization" in MCP_TOOL_NAMES
        assert "list_workspaces" in MCP_TOOL_NAMES
        assert "list_datasets" in MCP_TOOL_NAMES


class TestTeardownSchemaUnbound:
    """The destructive MCP ``teardown_schema`` tool must NOT be exposed to the agent.

    arch #237 / finding 00#2: the agent-facing ``teardown_schema`` MCP tool DROPs
    physical schemas but updates no Django state (TenantSchema stays ACTIVE, runs
    stay COMPLETED, sibling multi-tenant view schemas are never failed) and has no
    role/membership check — only an LLM-suppliable ``confirm`` flag. It duplicates
    the worker teardown task with none of its safety machinery, so it is unbound
    from the agent: the LLM can no longer call it.
    """

    def test_teardown_schema_not_in_mcp_tool_names(self):
        from apps.agents.graph.base import MCP_TOOL_NAMES

        assert "teardown_schema" not in MCP_TOOL_NAMES

    def test_teardown_schema_filtered_from_agent_tools(self):
        """``_build_tools`` must drop the MCP ``teardown_schema`` tool even though
        the MCP server still advertises it (operator/HTTP callers keep it)."""
        from apps.agents.graph.base import _build_tools

        def _fake_mcp_tool(name):
            t = MagicMock()
            t.name = name
            return t

        mcp_tools = [
            _fake_mcp_tool("semantic_query"),
            _fake_mcp_tool("list_tables"),
            _fake_mcp_tool("teardown_schema"),
        ]
        workspace = SimpleNamespace(id="ws-1", system_prompt="")

        tools = _build_tools(workspace, None, mcp_tools)
        tool_names = {t.name for t in tools}

        assert "teardown_schema" not in tool_names
        assert "semantic_query" in tool_names
        assert "list_tables" not in tool_names

    def test_parent_graph_exposes_artifact_manager_not_primitives(self):
        from apps.agents.graph.base import _build_tools

        workspace = SimpleNamespace(id="ws-1", system_prompt="")
        tools = _build_tools(workspace, None, [])
        tool_names = {t.name for t in tools}

        assert "artifact_manager" in tool_names
        assert "artifact_write" not in tool_names
        assert "artifact_graph_overview" not in tool_names
        assert "get_artifact_semantic_queries" not in tool_names

    def test_artifact_manager_tool_call_id_hidden_from_llm_schema(self):
        from apps.agents.graph.base import INJECTED_TOOL_PARAMS, _build_tools, _llm_tool_schemas

        workspace = SimpleNamespace(id="ws-1", system_prompt="")
        schemas = _llm_tool_schemas(
            _build_tools(workspace, None, []),
            hidden_params=list(INJECTED_TOOL_PARAMS),
        )
        artifact_schema = next(
            item
            for item in schemas
            if isinstance(item, dict)
            and item["function"]["name"] == "artifact_manager"
        )
        props = artifact_schema["function"]["parameters"]["properties"]

        assert "task" in props
        assert "tool_call_id" not in props
        assert "subagent_event_queue" not in props


class TestHeadlessMode:
    """interactive=False swaps the interactive MCP run_materialization (fire-and-
    ack) for the headless blocking tool, and emits blocking prompt guidance."""

    def _fake_mcp_tool(self, name):
        t = MagicMock()
        t.name = name
        return t

    def test_build_tools_headless_swaps_in_blocking_materialization(self):
        from apps.agents.graph.base import _build_tools

        mcp_tools = [
            self._fake_mcp_tool("semantic_query"),
            self._fake_mcp_tool("run_materialization"),
        ]
        workspace = SimpleNamespace(id="ws-1", system_prompt="")

        headless = _build_tools(workspace, None, mcp_tools, interactive=False, job_id=7)
        rm = [t for t in headless if t.name == "run_materialization"]
        assert len(rm) == 1, "exactly one run_materialization tool"
        # The headless tool replaces the MCP one (different object identity).
        assert rm[0] not in mcp_tools
        assert callable(getattr(rm[0], "coroutine", None))  # async StructuredTool

    def test_build_tools_interactive_keeps_mcp_materialization(self):
        from apps.agents.graph.base import _build_tools

        mcp_rm = self._fake_mcp_tool("run_materialization")
        mcp_tools = [self._fake_mcp_tool("semantic_query"), mcp_rm]
        workspace = SimpleNamespace(id="ws-1", system_prompt="")

        interactive = _build_tools(workspace, None, mcp_tools, interactive=True)
        rm = [t for t in interactive if t.name == "run_materialization"]
        assert rm == [mcp_rm], "interactive path keeps the original MCP tool untouched"

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_headless_no_data_prompt_is_blocking_not_resume(self, tenant, user):
        from apps.agents.graph.base import _fetch_schema_context

        interactive = await _fetch_schema_context(tenant, user, interactive=True)
        headless = await _fetch_schema_context(tenant, user, interactive=False)

        assert "run_materialization" in headless
        # Interactive tells the agent to end its turn and wait for an async resume.
        assert "end your turn" in interactive.lower()
        # Headless must NOT — it has no resume path; it blocks and continues.
        assert "end your turn" not in headless.lower()
        assert "block" in headless.lower()

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_headless_in_progress_prompt_does_not_trigger_second_materialization(
        self, tenant, user
    ):
        """When a materialization is already in progress, the headless prompt
        must NOT tell the agent "no data loaded → call run_materialization" (that
        starts a 2nd parallel run). It gets distinct in-progress guidance."""
        from apps.agents.graph.base import _fetch_schema_context
        from apps.workspaces.models import SchemaState, TenantSchema

        await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="t_inprog", state=SchemaState.MATERIALIZING
        )
        msg = await _fetch_schema_context(tenant, user, interactive=False)

        assert "run_materialization" in msg  # still the only path to data, headless
        assert "no data has been loaded" not in msg.lower()  # would imply "start one"
        assert "end your turn" not in msg.lower()  # no async resume in headless
        assert "in progress" in msg.lower()  # acknowledges the in-flight load


class TestSystemPrompt:
    """Verify the system prompt includes data availability instructions."""

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_data_availability_section_present(self, workspace, user, tenant):
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

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
    async def test_data_availability_covers_not_provisioned_case(self, workspace, user):
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

        # Agent must know to run materialization when no data exists
        assert "No data has been loaded yet" in prompt or "loading" in prompt.lower()
