import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from apps.agents.graph.base import SUBAGENT_EVENT_QUEUE_CONFIG_KEY, _make_injecting_tool_node
from apps.agents.graph.state import AgentState


def test_agent_state_has_thread_id_field():
    # TypedDict membership check.
    assert "thread_id" in AgentState.__annotations__


def test_make_injecting_tool_node_injects_thread_id_and_tool_call_id(monkeypatch):
    """The injecting wrapper copies state values + the LangChain tool_call_id
    into the tool call args for any MCP tool."""
    monkeypatch.setattr(
        "apps.agents.graph.base.MCP_TOOL_NAMES",
        {"run_materialization"},
    )

    captured_messages: list = []

    base_node = MagicMock()

    async def fake_ainvoke(payload, **kwargs):
        captured_messages.append(payload["messages"])
        return {"messages": []}

    base_node.ainvoke = AsyncMock(side_effect=fake_ainvoke)

    injections = {
        "workspace_id": "workspace_id",
        "user_id": "user_id",
        "thread_id": "thread_id",
    }
    node = _make_injecting_tool_node(base_node, injections)

    tool_call = {
        "name": "run_materialization",
        "id": "tc-abc-123",
        "args": {"foo": "bar"},
    }
    ai_msg = AIMessage(content="", tool_calls=[tool_call])

    state = {
        "messages": [ai_msg],
        "workspace_id": "ws-1",
        "user_id": "user-1",
        "thread_id": "thread-1",
    }

    asyncio.run(node(state))

    # Inspect the message that was passed through to the base ToolNode.
    forwarded = captured_messages[0]
    forwarded_ai = forwarded[-1]
    forwarded_args = forwarded_ai.tool_calls[0]["args"]
    assert forwarded_args["foo"] == "bar"
    assert forwarded_args["workspace_id"] == "ws-1"
    assert forwarded_args["user_id"] == "user-1"
    assert forwarded_args["thread_id"] == "thread-1"
    assert forwarded_args["tool_call_id"] == "tc-abc-123"


def test_make_injecting_tool_node_injects_subagent_queue_for_artifact_manager(monkeypatch):
    """The Artifact Manager local tool gets the active stream queue directly,
    so child events do not rely on ContextVar propagation inside ToolNode.
    """
    monkeypatch.setattr(
        "apps.agents.graph.base.LOCAL_CONTEXT_TOOL_NAMES",
        {"artifact_manager"},
    )

    captured_messages: list = []
    base_node = MagicMock()

    async def fake_ainvoke(payload, **kwargs):
        captured_messages.append(payload["messages"])
        return {"messages": []}

    base_node.ainvoke = AsyncMock(side_effect=fake_ainvoke)
    node = _make_injecting_tool_node(base_node, {})
    queue = asyncio.Queue()
    ai_msg = AIMessage(
        content="",
        tool_calls=[{"name": "artifact_manager", "id": "tc-art", "args": {"task": "build"}}],
    )

    asyncio.run(
        node(
            {"messages": [ai_msg]},
            config={"configurable": {SUBAGENT_EVENT_QUEUE_CONFIG_KEY: queue}},
        )
    )

    forwarded_args = captured_messages[0][-1].tool_calls[0]["args"]
    assert forwarded_args["tool_call_id"] == "tc-art"
    assert forwarded_args[SUBAGENT_EVENT_QUEUE_CONFIG_KEY] is queue


def test_make_injecting_tool_node_warns_on_missing_tool_call_id(monkeypatch, caplog):
    """A tool call without an id should produce a warning, not crash."""
    monkeypatch.setattr(
        "apps.agents.graph.base.MCP_TOOL_NAMES",
        {"run_materialization"},
    )
    base_node = MagicMock()
    base_node.ainvoke = AsyncMock(return_value={"messages": []})

    node = _make_injecting_tool_node(
        base_node,
        {"workspace_id": "workspace_id"},
    )
    tool_call = {"name": "run_materialization", "id": None, "args": {}}  # explicit None id
    ai_msg = AIMessage(content="", tool_calls=[tool_call])
    state = {"messages": [ai_msg], "workspace_id": "ws-1"}

    with caplog.at_level(logging.WARNING, logger="apps.agents.graph.base"):
        asyncio.run(node(state))

    assert any("has no id" in r.message for r in caplog.records)
