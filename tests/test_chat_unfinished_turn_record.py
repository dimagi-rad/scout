"""A turn that fails or is stopped leaves an assistant message saying so (#856).

A chat the user has left has nobody watching its stream, so the checkpoint is the
only place a failure can be seen. The record must also keep the history valid for
the model: a tool_use the turn never answered gets its tool_result before the
marker, not after it.
"""

import asyncio
import json
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from apps.chat import stream
from apps.chat.helpers import repair_dangling_tool_calls
from apps.chat.message_converter import langchain_messages_to_ui
from apps.common.capacity import CapacityExhausted, CapacityResource

CONFIG = {"configurable": {"thread_id": "t1"}}


def _events(chunks: list[str]) -> list[dict]:
    return [json.loads(c.removeprefix("data: ").strip()) for c in chunks]


class _ScriptedAgent:
    """Replays stream events, then raises; records the checkpoint write."""

    def __init__(self, events: list[dict], exc: BaseException, history: list | None = None):
        self._events = events
        self._exc = exc
        state = MagicMock()
        state.values = {"messages": history or []}
        self.aget_state = AsyncMock(return_value=state)
        self.aupdate_state = AsyncMock()

    def astream_events(self, input_state, *, config, version):
        async def _gen():
            for event in self._events:
                yield event
            raise self._exc

        return _gen()


def _text_event(text: str) -> dict:
    return {
        "event": "on_chat_model_stream",
        "data": {"chunk": type("Chunk", (), {"content": text})()},
    }


async def _run(agent, **kwargs) -> list[dict]:
    return _events([c async for c in stream.langgraph_to_ui_stream(agent, {}, CONFIG, **kwargs)])


def _written(agent) -> list:
    agent.aupdate_state.assert_awaited_once()
    args, kwargs = agent.aupdate_state.await_args
    assert kwargs["as_node"] == "agent"
    return args[1]["messages"]


@pytest.mark.asyncio
async def test_a_failed_turn_saves_a_marker_carrying_the_error_ref():
    agent = _ScriptedAgent([], ValueError("boom"))

    events = await _run(agent)

    error = next(e for e in events if e["type"] == "error")
    ref = error["errorText"].rsplit("Ref: ", 1)[1]
    [marker] = _written(agent)
    assert marker.content == f"_This response didn't finish. (Ref: {ref})_"
    assert marker.response_metadata == {"scout_turn_failed": True}


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_the_text_it_streamed():
    agent = _ScriptedAgent([_text_event("Half "), _text_event("an answer")], ValueError("x"))

    await _run(agent)

    [marker] = _written(agent)
    assert marker.content.startswith("Half an answer\n\n_This response didn't finish. (Ref: ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            stream.RateLimitError(
                "rate", response=MagicMock(status_code=429, headers={}), body=None
            ),
            id="overload",
        ),
        pytest.param(CapacityExhausted(CapacityResource.CUBE), id="capacity"),
    ],
)
async def test_a_retryable_failure_also_saves_the_marker(exc):
    agent = _ScriptedAgent([], exc)

    events = await _run(agent)

    assert any(e["type"] == "data-chat-status" for e in events)
    [marker] = _written(agent)
    assert marker.response_metadata == {"scout_turn_failed": True}


@pytest.mark.asyncio
async def test_a_full_checkpointer_pool_skips_the_write_it_could_not_make():
    agent = _ScriptedAgent([], CapacityExhausted(CapacityResource.CHECKPOINTER_POOL))

    events = await _run(agent)

    assert any(e["type"] == "data-chat-status" for e in events)
    agent.aupdate_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_that_lost_its_lease_does_not_write_the_marker():
    agent = _ScriptedAgent([_text_event("partial")], ValueError("boom"))

    events = await _run(agent, owns_thread=lambda: False)

    assert any(e["type"] == "error" for e in events)
    agent.aupdate_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_write_still_ends_the_stream():
    agent = _ScriptedAgent([], ValueError("boom"))
    agent.aupdate_state.side_effect = RuntimeError("checkpointer down")

    events = await _run(agent)

    assert events[-1] == {"type": "finish", "finishReason": "stop"}


@pytest.mark.asyncio
async def test_unanswered_tool_calls_are_answered_before_the_marker():
    history = [
        HumanMessage(content="q"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "query", "args": {}},
                {"id": "call_2", "name": "describe", "args": {}},
            ],
        ),
        ToolMessage(content="rows", tool_call_id="call_1", name="query"),
    ]
    agent = _ScriptedAgent([], ValueError("boom"), history=history)

    await _run(agent)

    interrupted, marker = _written(agent)
    assert isinstance(interrupted, ToolMessage)
    assert (interrupted.tool_call_id, interrupted.name) == ("call_2", "describe")
    assert marker.response_metadata == {"scout_turn_failed": True}


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
async def test_a_turn_failing_mid_tool_leaves_a_history_the_next_turn_can_send():
    async def failing_tool(state: _State) -> dict:
        raise RuntimeError("tool crashed")

    graph = _graph_whose_tool(failing_tool)

    async for _chunk in stream.langgraph_to_ui_stream(
        graph, {"messages": [HumanMessage(content="q")]}, CONFIG
    ):
        pass

    saved = (await graph.aget_state(CONFIG)).values["messages"]
    assert [type(m).__name__ for m in saved] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert saved[-1].response_metadata == {"scout_turn_failed": True}
    _assert_valid_for_the_model(await _next_turn_history(graph))

    _question, tool_turn, notice = langchain_messages_to_ui(saved)
    assert [p["state"] for p in tool_turn["parts"]] == ["output-available"]
    assert notice["role"] == "assistant"
    [text_part] = notice["parts"]
    assert text_part["type"] == "text"
    assert text_part["text"].startswith("_This response didn't finish. (Ref: ")


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
