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
        assert "query" in MCP_TOOL_NAMES
        assert "get_metadata" in MCP_TOOL_NAMES
        assert "run_materialization" in MCP_TOOL_NAMES


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
            _fake_mcp_tool("query"),
            _fake_mcp_tool("list_tables"),
            _fake_mcp_tool("teardown_schema"),
        ]
        workspace = SimpleNamespace(id="ws-1", system_prompt="")

        tools = _build_tools(workspace, None, mcp_tools)
        tool_names = {t.name for t in tools}

        assert "teardown_schema" not in tool_names
        # The non-destructive MCP tools survive.
        assert "query" in tool_names
        assert "list_tables" in tool_names


class TestSystemPrompt:
    """Verify the system prompt includes data availability instructions."""

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_data_availability_section_present(self, workspace, user):
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

        assert "Data Availability" in prompt
        # Schema context is now pre-fetched; no instruction to call get_schema_status
        assert "get_schema_status" not in prompt
        # When no schema exists, agent is told to call run_materialization
        assert "run_materialization" in prompt

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_data_availability_covers_not_provisioned_case(self, workspace, user):
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, user)

        # Agent must know to run materialization when no data exists
        assert "No data has been loaded yet" in prompt or "loading" in prompt.lower()
