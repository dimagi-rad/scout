"""Tests for the agent graph builder."""

import pytest


class TestMcpToolNames:
    """Verify MCP_TOOL_NAMES contains all tools that need tenant_id injection."""

    def test_new_tools_in_mcp_tool_names(self):
        from apps.agents.graph.base import MCP_TOOL_NAMES

        assert "get_schema_status" in MCP_TOOL_NAMES
        assert "teardown_schema" in MCP_TOOL_NAMES

    def test_existing_tools_still_present(self):
        from apps.agents.graph.base import MCP_TOOL_NAMES

        assert "list_tables" in MCP_TOOL_NAMES
        assert "describe_table" in MCP_TOOL_NAMES
        assert "query" in MCP_TOOL_NAMES
        assert "get_metadata" in MCP_TOOL_NAMES
        assert "run_materialization" in MCP_TOOL_NAMES


class TestSystemPrompt:
    """Verify the system prompt includes data availability instructions."""

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_data_availability_section_present(self, workspace, tenant_membership):
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, tenant_membership)

        assert "Data Availability" in prompt
        # Schema context is now pre-fetched; no instruction to call get_schema_status
        assert "get_schema_status" not in prompt
        # When no schema exists, agent is told to call run_materialization
        assert "run_materialization" in prompt

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_data_availability_covers_not_provisioned_case(self, workspace, tenant_membership):
        from apps.agents.graph.base import _build_system_prompt

        prompt = await _build_system_prompt(workspace, tenant_membership)

        # Agent must know to run materialization when no data exists
        assert "No data has been loaded yet" in prompt or "loading" in prompt.lower()
