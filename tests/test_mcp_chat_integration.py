"""
Integration tests for MCP endpoints via the chat UI.

Tests the full integration path from POST /api/chat/ through MCP tool loading,
LangGraph agent execution, and SSE streaming back to the client.

Mocking strategy:
- ChatAnthropic: mocked to control tool calls and text responses
- MCP server: mocked via get_mcp_tools() returning fake LangChain tools
- Checkpointer: uses MemorySaver (no PostgreSQL needed)
- Django ORM: real test DB with fixtures
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.contrib.auth.signals import user_logged_in
from django.test import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from apps.agents.graph.nodes import (
    _classify_error,
    _extract_error,
    check_result_node,
    diagnose_and_retry_node,
)
from apps.chat.stream import _sse, _tool_content_to_str, langgraph_to_ui_stream

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def async_client():
    """Django async test client."""
    return AsyncClient()


@pytest.fixture
def auth_async_client(async_client, user):
    """Authenticated async test client.

    Uses sync force_login with the update_last_login signal disconnected
    to avoid async transaction isolation issues.
    """
    # Disconnect update_last_login signal to avoid cross-transaction save
    from django.contrib.auth.models import update_last_login

    user_logged_in.disconnect(update_last_login)
    try:
        async_client.force_login(user)
    finally:
        user_logged_in.connect(update_last_login)
    return async_client


def _chat_body(tenant_id, message="What tables are available?", thread_id=None):
    """Build a chat request body."""
    body = {
        "messages": [{"role": "user", "content": message}],
        "data": {"tenantId": str(tenant_id)},
    }
    if thread_id:
        body["data"]["threadId"] = str(thread_id)
    return body


def _chat_body_v6(tenant_id, message="What tables are available?"):
    """Build a chat request body using AI SDK v6 parts format."""
    return {
        "messages": [
            {
                "role": "user",
                "parts": [{"type": "text", "text": message}],
            }
        ],
        "data": {"tenantId": str(tenant_id)},
    }


async def _collect_sse_events(response):
    """Collect SSE events from a StreamingHttpResponse."""
    events = []
    async for chunk in response.streaming_content:
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
        for line in text.strip().split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return events


# ---------------------------------------------------------------------------
# Layer 1: Chat Endpoint Validation
# ---------------------------------------------------------------------------


class TestChatEndpointValidation:
    """Test request validation in the chat view."""

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_unauthenticated_returns_401(self, async_client, tenant_membership):
        """Unauthenticated requests should return 401."""
        response = await async_client.post(
            "/api/chat/",
            data=json.dumps(_chat_body(tenant_membership.id)),
            content_type="application/json",
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_get_method_returns_405(self, auth_async_client, tenant_membership):
        """GET requests should return 405."""
        response = await auth_async_client.get("/api/chat/")
        assert response.status_code == 405

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_missing_messages_returns_400(self, auth_async_client, tenant_membership):
        """Missing messages field should return 400."""
        response = await auth_async_client.post(
            "/api/chat/",
            data=json.dumps({"data": {"tenantId": str(tenant_membership.id)}}),
            content_type="application/json",
        )
        assert response.status_code == 400
        body = json.loads(response.content)
        assert "messages" in body["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_missing_tenant_id_returns_400(self, auth_async_client):
        """Missing tenantId should return 400."""
        response = await auth_async_client.post(
            "/api/chat/",
            data=json.dumps({"messages": [{"content": "hello"}]}),
            content_type="application/json",
        )
        assert response.status_code == 400
        body = json.loads(response.content)
        assert "tenantId" in body["error"]

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_empty_message_returns_400(self, auth_async_client, tenant_membership):
        """Empty message content should return 400."""
        response = await auth_async_client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"content": ""}],
                    "data": {"tenantId": str(tenant_membership.id)},
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400
        body = json.loads(response.content)
        assert "empty" in body["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_whitespace_only_message_returns_400(self, auth_async_client, tenant_membership):
        """Whitespace-only message should return 400."""
        response = await auth_async_client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"content": "   \n\t  "}],
                    "data": {"tenantId": str(tenant_membership.id)},
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_message_too_long_returns_400(self, auth_async_client, tenant_membership):
        """Message exceeding MAX_MESSAGE_LENGTH should return 400."""
        long_msg = "x" * 10_001
        response = await auth_async_client.post(
            "/api/chat/",
            data=json.dumps(_chat_body(tenant_membership.id, message=long_msg)),
            content_type="application/json",
        )
        assert response.status_code == 400
        body = json.loads(response.content)
        assert "10000" in body["error"]

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_non_member_returns_403(self, auth_async_client):
        """User without tenant membership should get 403."""
        # No tenant_membership fixture — send a fake tenant ID
        fake_tenant_id = str(uuid.uuid4())
        response = await auth_async_client.post(
            "/api/chat/",
            data=json.dumps(_chat_body(fake_tenant_id)),
            content_type="application/json",
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_nonexistent_tenant_id_returns_403(self, auth_async_client):
        """Non-existent tenant ID should return 403."""
        fake_id = str(uuid.uuid4())
        response = await auth_async_client.post(
            "/api/chat/",
            data=json.dumps(_chat_body(fake_id)),
            content_type="application/json",
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_invalid_json_returns_400(self, auth_async_client):
        """Invalid JSON body should return 400."""
        response = await auth_async_client.post(
            "/api/chat/",
            data="not json",
            content_type="application/json",
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_v6_parts_format_accepted(self, auth_async_client, tenant_membership):
        """AI SDK v6 parts format should be accepted."""
        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mock_mcp,
            patch("apps.chat.views._ensure_checkpointer", new_callable=AsyncMock) as mock_cp,
            patch("apps.chat.views.build_agent_graph") as mock_build,
        ):
            mock_mcp.return_value = []
            from langgraph.checkpoint.memory import MemorySaver

            mock_cp.return_value = MemorySaver()

            # Mock the agent to return a simple text response
            mock_agent = AsyncMock()

            async def fake_events(*args, **kwargs):
                yield {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": MagicMock(content="Hello!")},
                }

            mock_agent.astream_events = fake_events
            mock_build.return_value = mock_agent

            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body_v6(tenant_membership.id)),
                content_type="application/json",
            )
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# Layer 2: MCP Tool Loading
# ---------------------------------------------------------------------------


class TestMCPToolLoading:
    """Test MCP tool loading in the chat view."""

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_mcp_tools_failure_returns_500(self, auth_async_client, tenant_membership):
        """When get_mcp_tools() raises, chat view should return 500."""
        with patch(
            "apps.chat.views.get_mcp_tools",
            new_callable=AsyncMock,
            side_effect=ConnectionError("MCP server unreachable"),
        ):
            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body(tenant_membership.id)),
                content_type="application/json",
            )
            assert response.status_code == 500
            body = json.loads(response.content)
            assert "Agent initialization failed" in body["error"]
            assert "Ref:" in body["error"]

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_mcp_tools_success_proceeds_to_agent(self, auth_async_client, tenant_membership):
        """When get_mcp_tools() succeeds, the agent should be built with those tools."""
        mock_tool = MagicMock()
        mock_tool.name = "query"

        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mock_mcp,
            patch("apps.chat.views._ensure_checkpointer", new_callable=AsyncMock) as mock_cp,
            patch("apps.chat.views.build_agent_graph") as mock_build,
        ):
            mock_mcp.return_value = [mock_tool]
            from langgraph.checkpoint.memory import MemorySaver

            mock_cp.return_value = MemorySaver()

            mock_agent = AsyncMock()

            async def fake_events(*args, **kwargs):
                yield {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": MagicMock(content="Hi")},
                }

            mock_agent.astream_events = fake_events
            mock_build.return_value = mock_agent

            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body(tenant_membership.id)),
                content_type="application/json",
            )
            assert response.status_code == 200

            # Verify MCP tools were passed to build_agent_graph
            mock_build.assert_called_once()
            call_kwargs = mock_build.call_args
            assert call_kwargs.kwargs.get("mcp_tools") == [mock_tool] or (
                len(call_kwargs.args) > 3 and call_kwargs.args[3] == [mock_tool]
            )


# ---------------------------------------------------------------------------
# Layer 3: Agent Graph Assembly
# ---------------------------------------------------------------------------


class TestAgentGraphAssembly:
    """Test that build_agent_graph correctly incorporates MCP tools."""

    def test_mcp_tools_included_in_tool_list(self, user, workspace):
        """MCP tools should be included alongside local tools."""
        from apps.agents.graph.base import _build_tools

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.name = "query"

        tools = _build_tools(workspace, user, [mock_mcp_tool])
        tool_names = [t.name for t in tools]

        # MCP tool should be first
        assert "query" in tool_names
        # Local tools should also be present
        assert "save_learning" in tool_names
        assert "create_artifact" in tool_names

    def test_empty_mcp_tools_only_local(self, user, workspace):
        """With empty MCP tools, only local tools should be present."""
        from apps.agents.graph.base import _build_tools

        tools = _build_tools(workspace, user, [])
        tool_names = [t.name for t in tools]

        # Only local tools
        assert "save_learning" in tool_names
        assert "create_artifact" in tool_names
        # No MCP tools
        assert "query" not in tool_names
        assert "list_tables" not in tool_names

    def test_multiple_mcp_tools_preserved(self, user, workspace):
        """Multiple MCP tools should all be included."""
        from apps.agents.graph.base import _build_tools

        mcp_tools = []
        for name in ["query", "list_tables", "describe_table", "get_metadata"]:
            t = MagicMock()
            t.name = name
            mcp_tools.append(t)

        tools = _build_tools(workspace, user, mcp_tools)
        tool_names = [t.name for t in tools]

        for name in ["query", "list_tables", "describe_table", "get_metadata"]:
            assert name in tool_names


# ---------------------------------------------------------------------------
# Layer 4: SSE Stream Format
# ---------------------------------------------------------------------------


class TestSSEStreamFormat:
    """Test the langgraph_to_ui_stream SSE output format."""

    @pytest.mark.asyncio
    async def test_text_only_stream(self):
        """Simple text response should produce correct SSE event sequence."""
        mock_agent = AsyncMock()

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": MagicMock(content="Hello world")},
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        # Verify event sequence
        types = [e["type"] for e in events]
        assert types[0] == "start"
        assert types[1] == "start-step"
        assert "text-start" in types
        assert "text-delta" in types
        assert "text-end" in types
        assert types[-2] == "finish-step"
        assert types[-1] == "finish"

        # Verify text content
        text_deltas = [e for e in events if e["type"] == "text-delta"]
        assert len(text_deltas) > 0
        assert text_deltas[0]["delta"] == "Hello world"

    @pytest.mark.asyncio
    async def test_tool_call_stream(self):
        """Tool call should produce tool-input-available and tool-output-available events."""
        mock_agent = AsyncMock()

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_tool_end",
                "run_id": "run-123",
                "name": "query",
                "data": {
                    "output": ToolMessage(
                        content='{"success": true, "data": {"columns": ["id"], "rows": [[1]]}}',
                        tool_call_id="call-123",
                        name="query",
                    ),
                },
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": MagicMock(content="Found 1 result.")},
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        types = [e["type"] for e in events]
        assert "tool-input-available" in types
        assert "tool-output-available" in types

        # Verify tool event content
        tool_input = next(e for e in events if e["type"] == "tool-input-available")
        assert tool_input["toolName"] == "query"
        assert tool_input["toolCallId"] == "run-123"

        tool_output = next(e for e in events if e["type"] == "tool-output-available")
        assert tool_output["toolCallId"] == "run-123"

    @pytest.mark.asyncio
    async def test_tool_output_truncated_to_2000_chars(self):
        """Tool output longer than 2000 chars should be truncated."""
        mock_agent = AsyncMock()
        long_output = "x" * 5000

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_tool_end",
                "run_id": "run-456",
                "name": "query",
                "data": {
                    "output": ToolMessage(
                        content=long_output,
                        tool_call_id="call-456",
                        name="query",
                    ),
                },
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        tool_output = next(e for e in events if e["type"] == "tool-output-available")
        assert tool_output["output"].startswith("x" * 2000)
        assert "truncated" in tool_output["output"]
        assert "5000 chars total" in tool_output["output"]

    @pytest.mark.asyncio
    async def test_duplicate_tool_events_deduplicated(self):
        """Duplicate run_id tool events should be filtered."""
        mock_agent = AsyncMock()

        async def fake_events(*args, **kwargs):
            for _ in range(3):
                yield {
                    "event": "on_tool_end",
                    "run_id": "run-dup",
                    "name": "query",
                    "data": {
                        "output": ToolMessage(
                            content="result",
                            tool_call_id="call-dup",
                            name="query",
                        ),
                    },
                }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        tool_events = [e for e in events if e["type"] == "tool-input-available"]
        assert len(tool_events) == 1

    @pytest.mark.asyncio
    async def test_error_during_streaming_handled(self):
        """Errors during streaming should produce error text and clean finish."""
        mock_agent = AsyncMock()

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": MagicMock(content="Starting...")},
            }
            raise RuntimeError("LLM connection lost")

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        types = [e["type"] for e in events]

        # Should have error message
        error_deltas = [
            e for e in events if e["type"] == "text-delta" and "error" in e.get("delta", "").lower()
        ]
        assert len(error_deltas) > 0

        # Should still finish cleanly
        assert types[-2] == "finish-step"
        assert types[-1] == "finish"

    @pytest.mark.asyncio
    async def test_reasoning_blocks_emitted(self):
        """Thinking/reasoning blocks should produce reasoning events."""
        mock_agent = AsyncMock()

        chunk_with_thinking = MagicMock()
        chunk_with_thinking.content = [
            {"type": "thinking", "thinking": "Let me analyze this..."},
            {"type": "text", "text": "Here is my answer."},
        ]

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": chunk_with_thinking},
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        types = [e["type"] for e in events]
        assert "reasoning-start" in types
        assert "reasoning-delta" in types
        assert "reasoning-end" in types
        assert "text-start" in types
        assert "text-delta" in types

    @pytest.mark.asyncio
    async def test_empty_content_chunks_skipped(self):
        """Chunks with no content should be skipped."""
        mock_agent = AsyncMock()

        async def fake_events(*args, **kwargs):
            # Empty content chunk
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": MagicMock(content="")},
            }
            # None content
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": MagicMock(content=None)},
            }
            # Actual content
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": MagicMock(content="Real content")},
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        text_deltas = [e for e in events if e["type"] == "text-delta"]
        assert len(text_deltas) == 1
        assert text_deltas[0]["delta"] == "Real content"

    @pytest.mark.asyncio
    async def test_sse_format_correct(self):
        """Each SSE event should be formatted as 'data: {json}\\n\\n'."""
        chunk = _sse({"type": "start"})
        assert chunk == 'data: {"type": "start"}\n\n'

    @pytest.mark.asyncio
    async def test_tool_content_to_str_handles_types(self):
        """_tool_content_to_str should handle ToolMessage, string, and other types."""
        # ToolMessage
        msg = ToolMessage(content="result text", tool_call_id="x", name="test")
        assert _tool_content_to_str(msg) == "result text"

        # String
        assert _tool_content_to_str("hello") == "hello"

        # Dict
        assert _tool_content_to_str({"key": "value"}) == "{'key': 'value'}"


# ---------------------------------------------------------------------------
# Layer 5: MCP Error → Self-Correction Loop
# ---------------------------------------------------------------------------


class TestMCPErrorCorrection:
    """Test the error handling chain for MCP envelope errors."""

    def _make_tool_state(self, tool_content, tool_name="query"):
        """Build a minimal agent state with an AIMessage + ToolMessage."""
        content = tool_content if isinstance(tool_content, str) else json.dumps(tool_content)
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "call_1", "name": tool_name, "args": {}}],
                ),
                ToolMessage(content=content, tool_call_id="call_1", name=tool_name),
            ],
            "needs_correction": False,
            "correction_context": {},
        }

    def test_mcp_query_error_triggers_correction(self):
        """MCP query error envelope should trigger needs_correction."""
        state = self._make_tool_state(
            {
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Only SELECT statements are allowed",
                },
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is True
        assert "Only SELECT" in result["correction_context"]["error_message"]
        assert result["correction_context"]["tool_name"] == "query"

    def test_mcp_not_found_error_triggers_correction(self):
        """MCP NOT_FOUND error should trigger correction with table_not_found type."""
        state = self._make_tool_state(
            {
                "success": False,
                "error": {
                    "code": "NOT_FOUND",
                    "message": "Table 'usr' not found",
                    "detail": "Did you mean: users, user_roles",
                },
            },
            tool_name="describe_table",
        )
        result = check_result_node(state)
        assert result["needs_correction"] is True
        assert result["correction_context"]["error_type"] == "table_not_found"

    def test_mcp_timeout_error_classified_correctly(self):
        """MCP QUERY_TIMEOUT should be classified as 'timeout'."""
        state = self._make_tool_state(
            {
                "success": False,
                "error": {
                    "code": "QUERY_TIMEOUT",
                    "message": "Query timed out after 30 seconds",
                },
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is True
        assert result["correction_context"]["error_type"] == "timeout"

    def test_mcp_connection_error_triggers_correction(self):
        """MCP CONNECTION_ERROR should trigger correction."""
        state = self._make_tool_state(
            {
                "success": False,
                "error": {
                    "code": "CONNECTION_ERROR",
                    "message": "could not connect to server",
                },
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is True

    def test_mcp_success_no_correction(self):
        """MCP success envelope should not trigger correction."""
        state = self._make_tool_state(
            {
                "success": True,
                "data": {
                    "columns": ["id", "name"],
                    "rows": [[1, "Alice"]],
                    "row_count": 1,
                },
                "project_id": "abc",
                "schema": "public",
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is False

    def test_mcp_success_with_warnings_no_correction(self):
        """MCP success with warnings should not trigger correction."""
        state = self._make_tool_state(
            {
                "success": True,
                "data": {
                    "columns": ["id"],
                    "rows": [[i] for i in range(500)],
                    "row_count": 500,
                    "truncated": True,
                },
                "warnings": ["Results truncated to 500 rows"],
                "project_id": "abc",
                "schema": "public",
            }
        )
        result = check_result_node(state)
        assert result["needs_correction"] is False

    def test_diagnose_and_retry_increments_count(self):
        """diagnose_and_retry_node should increment retry_count."""
        state = {
            "messages": [HumanMessage(content="query")],
            "needs_correction": True,
            "retry_count": 0,
            "correction_context": {
                "error_type": "column_not_found",
                "error_message": "column 'usr_id' does not exist",
                "tool_name": "query",
                "failed_sql": "SELECT usr_id FROM users",
                "tables_accessed": ["users"],
            },
        }
        result = diagnose_and_retry_node(state)
        assert result["retry_count"] == 1
        assert result["needs_correction"] is False
        # Should inject a diagnostic SystemMessage
        new_messages = result["messages"]
        assert any(isinstance(m, SystemMessage) for m in new_messages)

    def test_max_retries_gives_up(self):
        """After MAX_RETRIES, agent should give up and explain."""
        state = {
            "messages": [HumanMessage(content="query")],
            "needs_correction": True,
            "retry_count": 3,
            "correction_context": {
                "error_type": "column_not_found",
                "error_message": "column 'usr_id' does not exist",
                "tool_name": "query",
                "failed_sql": "SELECT usr_id FROM users",
                "tables_accessed": ["users"],
            },
        }
        result = diagnose_and_retry_node(state)
        assert result["retry_count"] == 3  # Not incremented
        assert result["needs_correction"] is False
        # Give-up message should mention explaining to the user
        last_msg = result["messages"][-1]
        assert isinstance(last_msg, SystemMessage)
        assert "explain" in last_msg.content.lower()

    def test_error_classification_syntax(self):
        assert _classify_error("syntax error at or near 'SELCT'") == "syntax"

    def test_error_classification_column_not_found(self):
        assert _classify_error("column 'usr_id' does not exist") == "column_not_found"

    def test_error_classification_table_not_found(self):
        assert _classify_error('relation "nonexistent" does not exist') == "table_not_found"

    def test_error_classification_permission(self):
        assert _classify_error("permission denied for table users") == "permission"

    def test_error_classification_timeout(self):
        assert _classify_error("Query timed out after 30 seconds") == "timeout"

    def test_error_classification_type_mismatch(self):
        assert _classify_error("type mismatch: cannot compare integer and text") == "type_mismatch"

    def test_error_classification_generic(self):
        assert _classify_error("something unexpected happened") == "execution"

    def test_extract_error_from_mcp_envelope(self):
        """_extract_error should handle MCP envelope format."""
        result = {
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "Bad query"},
        }
        assert _extract_error(result) == "Bad query"

    def test_extract_error_returns_none_for_success(self):
        """_extract_error should return None for successful results."""
        result = {"success": True, "data": {}}
        assert _extract_error(result) is None


# ---------------------------------------------------------------------------
# Layer 6: End-to-End Streaming
# ---------------------------------------------------------------------------


class TestEndToEndStreaming:
    """Test the full chat view → SSE streaming path."""

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_full_text_response_stream(self, auth_async_client, tenant_membership):
        """Full path: chat request → text SSE stream."""
        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mock_mcp,
            patch("apps.chat.views._ensure_checkpointer", new_callable=AsyncMock) as mock_cp,
            patch("apps.chat.views.build_agent_graph") as mock_build,
        ):
            mock_mcp.return_value = []
            from langgraph.checkpoint.memory import MemorySaver

            mock_cp.return_value = MemorySaver()

            mock_agent = AsyncMock()

            async def fake_events(*args, **kwargs):
                yield {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": MagicMock(content="There are 5 tables available.")},
                }

            mock_agent.astream_events = fake_events
            mock_build.return_value = mock_agent

            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body(tenant_membership.id)),
                content_type="application/json",
            )

            assert response.status_code == 200
            assert response["Content-Type"] == "text/event-stream; charset=utf-8"
            assert response["Cache-Control"] == "no-cache"

            events = await _collect_sse_events(response)
            types = [e["type"] for e in events]

            assert types[0] == "start"
            assert "text-delta" in types
            assert types[-1] == "finish"

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_full_tool_call_stream(self, auth_async_client, tenant_membership):
        """Full path: chat request → tool call → tool result → text → SSE stream."""
        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mock_mcp,
            patch("apps.chat.views._ensure_checkpointer", new_callable=AsyncMock) as mock_cp,
            patch("apps.chat.views.build_agent_graph") as mock_build,
        ):
            mock_mcp.return_value = []
            from langgraph.checkpoint.memory import MemorySaver

            mock_cp.return_value = MemorySaver()

            mock_agent = AsyncMock()

            async def fake_events(*args, **kwargs):
                # Tool call
                yield {
                    "event": "on_tool_end",
                    "run_id": "run-abc",
                    "name": "list_tables",
                    "data": {
                        "output": ToolMessage(
                            content=json.dumps(
                                {
                                    "success": True,
                                    "data": {"tables": [{"name": "users", "type": "table"}]},
                                }
                            ),
                            tool_call_id="call-abc",
                            name="list_tables",
                        ),
                    },
                }
                # Text response
                yield {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": MagicMock(content="Found the users table.")},
                }

            mock_agent.astream_events = fake_events
            mock_build.return_value = mock_agent

            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body(tenant_membership.id)),
                content_type="application/json",
            )

            assert response.status_code == 200
            events = await _collect_sse_events(response)
            types = [e["type"] for e in events]

            assert "tool-input-available" in types
            assert "tool-output-available" in types
            assert "text-delta" in types

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_thread_created_on_chat(self, auth_async_client, tenant_membership):
        """A Thread record should be created when chatting."""
        from apps.chat.models import Thread

        thread_id = str(uuid.uuid4())

        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mock_mcp,
            patch("apps.chat.views._ensure_checkpointer", new_callable=AsyncMock) as mock_cp,
            patch("apps.chat.views.build_agent_graph") as mock_build,
        ):
            mock_mcp.return_value = []
            from langgraph.checkpoint.memory import MemorySaver

            mock_cp.return_value = MemorySaver()

            mock_agent = AsyncMock()

            async def fake_events(*args, **kwargs):
                yield {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": MagicMock(content="Hello")},
                }

            mock_agent.astream_events = fake_events
            mock_build.return_value = mock_agent

            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body(tenant_membership.id, thread_id=thread_id)),
                content_type="application/json",
            )

            assert response.status_code == 200

            # Consume the stream to ensure the view fully executes
            await _collect_sse_events(response)

            # Verify thread was created
            thread = await Thread.objects.filter(id=thread_id).afirst()
            assert thread is not None
            assert str(thread.tenant_membership_id) == str(tenant_membership.id)

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_agent_build_failure_returns_500(self, auth_async_client, tenant_membership):
        """If agent build fails, should return 500."""
        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mock_mcp,
            patch("apps.chat.views._ensure_checkpointer", new_callable=AsyncMock) as mock_cp,
            patch("apps.chat.views.build_agent_graph") as mock_build,
        ):
            mock_mcp.return_value = []
            from langgraph.checkpoint.memory import MemorySaver

            mock_cp.return_value = MemorySaver()
            mock_build.side_effect = RuntimeError("Agent build failed")

            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body(tenant_membership.id)),
                content_type="application/json",
            )
            assert response.status_code == 500
            body = json.loads(response.content)
            assert "Agent initialization failed" in body["error"]

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_checkpointer_retry_on_failure(self, auth_async_client, tenant_membership):
        """If first checkpointer fails, should retry with force_new=True."""
        call_count = 0

        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mock_mcp,
            patch("apps.chat.views._ensure_checkpointer", new_callable=AsyncMock) as mock_cp,
            patch("apps.chat.views.build_agent_graph") as mock_build,
        ):
            mock_mcp.return_value = []
            from langgraph.checkpoint.memory import MemorySaver

            mock_cp.return_value = MemorySaver()

            def build_side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise ConnectionError("Stale checkpointer connection")
                # Second call succeeds
                mock_agent = AsyncMock()

                async def fake_events(*a, **kw):
                    yield {
                        "event": "on_chat_model_stream",
                        "data": {"chunk": MagicMock(content="Recovered!")},
                    }

                mock_agent.astream_events = fake_events
                return mock_agent

            mock_build.side_effect = build_side_effect

            response = await auth_async_client.post(
                "/api/chat/",
                data=json.dumps(_chat_body(tenant_membership.id)),
                content_type="application/json",
            )

            assert response.status_code == 200
            assert call_count == 2  # First failed, second succeeded
