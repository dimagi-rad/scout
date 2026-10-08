"""A stopped turn's marker keeps the history valid for the model.

A tool_use the turn never answered gets its tool_result before the marker, not
after it, where the next turn's repair would miss it.
"""

import asyncio
from typing import Annotated

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from apps.chat import stream
from apps.chat.helpers import repair_dangling_tool_calls

CONFIG = {"configurable": {"thread_id": "t1"}}


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _graph_whose_tool(tool_behavior):
    """A real checkpointed graph whose agent calls one tool, run as ``tool_behavior``."""

    def agent(state: _State) -> dict:
        if isinstance(state["messages"][-1], ToolMessage):
            return {"messages": [AIMessage(content="done")]}
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "toolu_1", "name": "run_query", "args": {}}],
                )
            ]
        }

    def route(state: _State) -> str:
        return "tools" if state["messages"][-1].tool_calls else END

    builder = StateGraph(_State)
    builder.add_node("agent", agent)
    builder.add_node("tools", tool_behavior)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, ["tools", END])
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=InMemorySaver())


def _assert_valid_for_the_model(messages: list) -> None:
    """Each tool_use is answered by the messages straight after its AIMessage."""
    for i, message in enumerate(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            following = set()
            for later in messages[i + 1 :]:
                if not isinstance(later, ToolMessage):
                    break
                following.add(later.tool_call_id)
            assert {tc["id"] for tc in message.tool_calls} <= following, messages


async def _next_turn_history(graph) -> list:
    repairs = await repair_dangling_tool_calls(graph, CONFIG)
    state = await graph.aget_state(CONFIG)
    return [*state.values["messages"], *repairs, HumanMessage(content="next")]


@pytest.mark.asyncio
async def test_a_turn_stopped_mid_tool_leaves_a_history_the_next_turn_can_send():
    tool_started = asyncio.Event()

    async def hanging_tool(state: _State) -> dict:
        tool_started.set()
        await asyncio.Event().wait()
        return {}

    graph = _graph_whose_tool(hanging_tool)

    async def consume():
        async for _chunk in stream.langgraph_to_ui_stream(
            graph, {"messages": [HumanMessage(content="q")]}, CONFIG
        ):
            pass

    task = asyncio.create_task(consume())
    async with asyncio.timeout(30):
        await tool_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = (await graph.aget_state(CONFIG)).values["messages"]
    assert isinstance(saved[-2], ToolMessage)
    assert saved[-2].tool_call_id == "toolu_1"
    assert saved[-1].response_metadata == {"scout_response_stopped": True}
    _assert_valid_for_the_model(await _next_turn_history(graph))
