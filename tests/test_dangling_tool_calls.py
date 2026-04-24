"""Regression tests for dangling `tool_use` / `tool_result` pair repair.

If a tool call is interrupted (client disconnect, uvicorn restart, stream
timeout) between ``agent_node``'s checkpoint write and ``tool_node``'s, the
checkpoint is left with an ``AIMessage`` whose ``tool_calls`` have no
corresponding ``ToolMessage``. On the next turn, sending that history to
Anthropic raises a 400:

    ``tool_use`` ids were found without ``tool_result`` blocks immediately after

These tests cover the two-layer defense against that state.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.chat.helpers import repair_dangling_tool_calls


def _mock_agent_with_state(messages):
    """Build a MagicMock agent whose ``aget_state`` returns the given messages."""
    agent = MagicMock()
    state = MagicMock()
    state.values = {"messages": messages}
    agent.aget_state = AsyncMock(return_value=state)
    return agent


CONFIG = {"configurable": {"thread_id": "t1"}}


class TestRepairDanglingToolCalls:
    """Unit tests for ``repair_dangling_tool_calls`` in ``apps.chat.helpers``."""

    @pytest.mark.asyncio
    async def test_empty_state_returns_no_repairs(self):
        agent = _mock_agent_with_state([])
        assert await repair_dangling_tool_calls(agent, CONFIG) == []

    @pytest.mark.asyncio
    async def test_resolved_tool_calls_returns_no_repairs(self):
        messages = [
            HumanMessage(content="hello"),
            AIMessage(
                content="calling tool",
                tool_calls=[{"id": "call_1", "name": "list_tables", "args": {}}],
            ),
            ToolMessage(content="result", tool_call_id="call_1", name="list_tables"),
        ]
        agent = _mock_agent_with_state(messages)
        assert await repair_dangling_tool_calls(agent, CONFIG) == []

    @pytest.mark.asyncio
    async def test_dangling_tool_call_produces_synthetic_tool_message(self):
        messages = [
            HumanMessage(content="hello"),
            AIMessage(
                content="calling tool",
                tool_calls=[{"id": "call_1", "name": "list_tables", "args": {}}],
            ),
        ]
        agent = _mock_agent_with_state(messages)

        result = await repair_dangling_tool_calls(agent, CONFIG)

        assert len(result) == 1
        assert isinstance(result[0], ToolMessage)
        assert result[0].tool_call_id == "call_1"
        assert result[0].name == "list_tables"

    @pytest.mark.asyncio
    async def test_multiple_dangling_tool_calls_in_last_message(self):
        messages = [
            HumanMessage(content="hello"),
            AIMessage(
                content="calling tools",
                tool_calls=[
                    {"id": "call_1", "name": "list_tables", "args": {}},
                    {"id": "call_2", "name": "describe_table", "args": {"table": "t"}},
                ],
            ),
        ]
        agent = _mock_agent_with_state(messages)

        result = await repair_dangling_tool_calls(agent, CONFIG)

        assert {m.tool_call_id for m in result} == {"call_1", "call_2"}

    @pytest.mark.asyncio
    async def test_partial_resolution_only_repairs_missing(self):
        """AIMessage with two tool_calls, only one answered — one synthetic msg."""
        messages = [
            AIMessage(
                content="calling tools",
                tool_calls=[
                    {"id": "call_1", "name": "list_tables", "args": {}},
                    {"id": "call_2", "name": "describe_table", "args": {"table": "t"}},
                ],
            ),
            ToolMessage(content="result_1", tool_call_id="call_1", name="list_tables"),
        ]
        agent = _mock_agent_with_state(messages)

        result = await repair_dangling_tool_calls(agent, CONFIG)

        assert len(result) == 1
        assert result[0].tool_call_id == "call_2"

    @pytest.mark.asyncio
    async def test_aget_state_failure_returns_empty(self):
        """Checkpointer errors must not crash the chat view — fall back gracefully."""
        agent = MagicMock()
        agent.aget_state = AsyncMock(side_effect=RuntimeError("db down"))
        assert await repair_dangling_tool_calls(agent, CONFIG) == []

    @pytest.mark.asyncio
    async def test_non_dict_state_values_returns_empty(self):
        """Guard against unexpected state shapes — real LangGraph returns a
        StateSnapshot with .values as a dict, but defensive tests may pass a
        fully-AsyncMock'd agent where every attribute is itself a mock."""
        agent = AsyncMock()  # children auto-expand to AsyncMock ⇒ .get returns coroutines
        assert await repair_dangling_tool_calls(agent, CONFIG) == []


class TestAgentNodeGuard:
    """Verify ``agent_node``'s in-memory defense against dangling tool_calls.

    Even if ``repair_dangling_tool_calls`` misses one (races, bypassed path),
    ``agent_node`` must not send an invalid tool_use/tool_result sequence to
    the Anthropic API.
    """

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_agent_node_injects_synthetic_tool_result(self, workspace, user):
        """agent_node injects a synthetic ToolMessage after an orphan AIMessage
        before calling the LLM."""
        from apps.agents.graph.base import build_agent_graph

        captured: list = []

        async def fake_ainvoke(messages, *args, **kwargs):
            captured.extend(messages)
            return AIMessage(content="acknowledged", id="ai-resp-1")

        mock_bound = MagicMock()
        mock_bound.ainvoke = AsyncMock(side_effect=fake_ainvoke)
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value = mock_bound

        with patch("apps.agents.graph.base.ChatAnthropic", return_value=mock_llm):
            agent = await build_agent_graph(workspace=workspace, user=user, mcp_tools=[])

            orphan_state = {
                "messages": [
                    HumanMessage(content="hello"),
                    AIMessage(
                        content="calling tool",
                        tool_calls=[{"id": "call_1", "name": "list_tables", "args": {}}],
                    ),
                    HumanMessage(content="are you there?"),
                ],
                "workspace_id": str(workspace.id),
                "user_id": str(user.id),
                "user_role": "analyst",
            }

            await agent.ainvoke(orphan_state)

        tool_msgs = [m for m in captured if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1, f"Expected 1 synthetic ToolMessage, got {len(tool_msgs)}"
        assert tool_msgs[0].tool_call_id == "call_1"

        # Synthetic ToolMessage must sit immediately after its parent AIMessage.
        ai_idx = next(
            i
            for i, m in enumerate(captured)
            if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
        )
        tm_idx = captured.index(tool_msgs[0])
        assert tm_idx == ai_idx + 1, "Synthetic ToolMessage must follow its AIMessage directly"
