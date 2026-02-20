"""
Tests for MCP client integration and check_result_node envelope handling.

Covers:
- MCP client singleton creation
- check_result_node handling of MCP envelope error format
- check_result_node handling of direct error format (backwards compat)
- _extract_error helper for both formats
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from apps.agents.graph.nodes import _extract_error, check_result_node

# --- _extract_error tests ---


class TestExtractError:
    """Test the _extract_error helper for both error formats."""

    def test_mcp_envelope_error(self):
        """MCP envelope: {success: false, error: {code, message}}."""
        result = {
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "Only SELECT allowed"},
        }
        assert _extract_error(result) == "Only SELECT allowed"

    def test_mcp_envelope_error_with_detail(self):
        """MCP envelope with detail field."""
        result = {
            "success": False,
            "error": {
                "code": "NOT_FOUND",
                "message": "Table 'foo' not found",
                "detail": "Did you mean: foobar",
            },
        }
        assert _extract_error(result) == "Table 'foo' not found"

    def test_direct_error_string(self):
        """Direct format: {error: "message string"}."""
        result = {"error": "column 'usr_id' does not exist"}
        assert _extract_error(result) == "column 'usr_id' does not exist"

    def test_no_error(self):
        """No error present in result."""
        result = {"columns": ["id"], "rows": [[1]], "row_count": 1}
        assert _extract_error(result) is None

    def test_success_true_no_error(self):
        """MCP envelope with success=True has no error."""
        result = {
            "success": True,
            "data": {"columns": ["id"], "rows": [[1]]},
        }
        assert _extract_error(result) is None

    def test_success_false_string_error(self):
        """MCP envelope with non-dict error value."""
        result = {"success": False, "error": "something went wrong"}
        assert _extract_error(result) == "something went wrong"

    def test_error_none_value(self):
        """Direct format with error=None."""
        result = {"error": None, "columns": ["id"]}
        assert _extract_error(result) is None


# --- check_result_node tests ---


class TestCheckResultNode:
    """Test check_result_node with both error formats."""

    def _make_state(self, tool_content, tool_name="query", status=None):
        """Helper to build a minimal agent state with a ToolMessage."""
        msg = ToolMessage(
            content=tool_content if isinstance(tool_content, str) else json.dumps(tool_content),
            tool_call_id="call_123",
            name=tool_name,
        )
        if status:
            msg.status = status
        return {
            "messages": [
                AIMessage(
                    content="", tool_calls=[{"id": "call_123", "name": tool_name, "args": {}}]
                ),
                msg,
            ],
            "needs_correction": False,
            "correction_context": {},
        }

    def test_mcp_envelope_error_triggers_correction(self):
        """MCP envelope error should trigger needs_correction."""
        state = self._make_state(
            {
                "success": False,
                "error": {"code": "VALIDATION_ERROR", "message": "Only SELECT allowed"},
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is True
        assert result["correction_context"]["error_message"] == "Only SELECT allowed"
        assert result["correction_context"]["tool_name"] == "query"

    def test_direct_error_triggers_correction(self):
        """Direct error format should still trigger needs_correction."""
        state = self._make_state(
            {
                "error": "column 'usr_id' does not exist",
                "sql_executed": "SELECT usr_id FROM users",
                "tables_accessed": ["users"],
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is True
        assert result["correction_context"]["error_message"] == "column 'usr_id' does not exist"
        assert result["correction_context"]["failed_sql"] == "SELECT usr_id FROM users"
        assert result["correction_context"]["tables_accessed"] == ["users"]

    def test_mcp_envelope_success_no_correction(self):
        """MCP envelope with success=True should not trigger correction."""
        state = self._make_state(
            {
                "success": True,
                "data": {"columns": ["id"], "rows": [[1]], "row_count": 1},
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is False

    def test_successful_direct_result_no_correction(self):
        """Direct result with no error should not trigger correction."""
        state = self._make_state(
            {
                "columns": ["id", "name"],
                "rows": [[1, "Alice"]],
                "row_count": 1,
                "sql_executed": "SELECT id, name FROM users",
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is False

    def test_mcp_envelope_extracts_data_fields(self):
        """MCP envelope error with data should extract sql and tables from data."""
        state = self._make_state(
            {
                "success": False,
                "error": {"code": "QUERY_TIMEOUT", "message": "Query timed out"},
                "data": {
                    "sql_executed": "SELECT * FROM big_table",
                    "tables_accessed": ["big_table"],
                },
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is True
        assert result["correction_context"]["failed_sql"] == "SELECT * FROM big_table"
        assert result["correction_context"]["tables_accessed"] == ["big_table"]

    def test_non_json_text_no_correction(self):
        """Plain text tool result should not trigger correction."""
        msg = ToolMessage(
            content="Table 'users' has 5 columns and 1000 rows.",
            tool_call_id="call_456",
            name="describe_table",
        )
        state = {
            "messages": [msg],
            "needs_correction": False,
            "correction_context": {},
        }
        result = check_result_node(state)
        assert result["needs_correction"] is False

    def test_error_status_non_json_triggers_correction(self):
        """Non-JSON content with error status should trigger correction."""
        msg = ToolMessage(
            content="ConnectionError: could not connect to server",
            tool_call_id="call_789",
            name="query",
            status="error",
        )
        state = {
            "messages": [msg],
            "needs_correction": False,
            "correction_context": {},
        }
        result = check_result_node(state)
        assert result["needs_correction"] is True
        assert "could not connect" in result["correction_context"]["error_message"]

    def test_empty_messages(self):
        """Empty messages should not trigger correction."""
        state = {"messages": [], "needs_correction": False, "correction_context": {}}
        result = check_result_node(state)
        assert result["needs_correction"] is False

    def test_non_tool_message(self):
        """Non-ToolMessage should not trigger correction."""
        state = {
            "messages": [AIMessage(content="Hello")],
            "needs_correction": False,
            "correction_context": {},
        }
        result = check_result_node(state)
        assert result["needs_correction"] is False

    def test_error_classification_preserved(self):
        """Error type classification should work with MCP envelope format."""
        state = self._make_state(
            {
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": 'relation "nonexistent" does not exist',
                },
            }
        )
        result = check_result_node(state)
        assert result["correction_context"]["error_type"] == "table_not_found"


# --- MCP client singleton tests ---


class TestMCPClient:
    @pytest.mark.asyncio
    async def test_get_mcp_client_creates_singleton(self):
        """get_mcp_client should return the same instance on repeated calls."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        with patch.object(mod, "settings") as mock_settings:
            mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"

            client1 = await mod.get_mcp_client()
            client2 = await mod.get_mcp_client()
            assert client1 is client2

        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_calls_get_tools(self):
        """get_mcp_tools should call client.get_tools()."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_tool = AsyncMock()
        mock_tool.name = "query"
        mock_client.get_tools.return_value = [mock_tool]

        mod._mcp_client = mock_client

        tools = await mod.get_mcp_tools()
        assert len(tools) == 1
        assert tools[0].name == "query"
        mock_client.get_tools.assert_awaited_once()

        mod.reset_circuit_breaker()
