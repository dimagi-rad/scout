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
@pytest.mark.parametrize(
    "resource", [CapacityResource.CHECKPOINTER_POOL, CapacityResource.DATABASE]
)
async def test_no_spare_connection_skips_the_write_it_could_not_make(resource):
    agent = _ScriptedAgent([], CapacityExhausted(resource))

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


class _SlowWrite:
    """An ``aupdate_state`` that holds until released, recording whether it landed."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.landed = False

    async def __call__(self, *_args, **_kwargs):
        self.started.set()
        await self.release.wait()
        self.landed = True


async def _cancel_during_the_write(agent, write: _SlowWrite, owns_thread) -> asyncio.Task:
    async def consume():
        async for _chunk in stream.langgraph_to_ui_stream(
            agent, {}, CONFIG, owns_thread=owns_thread
        ):
            pass

    task = asyncio.create_task(consume())
    async with asyncio.timeout(10):
        await write.started.wait()
    task.cancel()
    await asyncio.sleep(0.05)
    return task


@pytest.mark.asyncio
async def test_a_cancelled_stream_ends_only_once_its_write_has_landed():
    """The turn lease is released when the stream ends, so the write must land first."""
    agent = _ScriptedAgent([], ValueError("boom"))
    write = _SlowWrite()
    agent.aupdate_state = write

    task = await _cancel_during_the_write(agent, write, owns_thread=lambda: True)

    assert not task.done()
    write.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert write.landed


@pytest.mark.asyncio
async def test_a_stream_cancelled_for_losing_its_lease_drops_its_write():
    agent = _ScriptedAgent([], ValueError("boom"))
    write = _SlowWrite()
    agent.aupdate_state = write
    owns = True

    async def consume_then_lose_lease():
        nonlocal owns
        await write.started.wait()
        owns = False

    losing = asyncio.create_task(consume_then_lose_lease())
    task = await _cancel_during_the_write(agent, write, owns_thread=lambda: owns)
    await losing

    # Well inside the write timeout: the write is dropped, not waited out.
    await asyncio.wait({task}, timeout=1)
    assert task.cancelled()
    write.release.set()
    await asyncio.sleep(0)
    assert not write.landed


@pytest.mark.asyncio
async def test_a_write_that_hangs_gives_up_and_the_stream_ends(monkeypatch):
    monkeypatch.setattr(stream, "TERMINAL_WRITE_TIMEOUT_SECONDS", 0.05)
    agent = _ScriptedAgent([], ValueError("boom"))
    agent.aupdate_state = _SlowWrite()

    async with asyncio.timeout(10):
        events = await _run(agent)

    assert events[-1] == {"type": "finish", "finishReason": "stop"}
    assert not agent.aupdate_state.landed


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
    assert interrupted.status == "error"
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


@pytest.mark.asyncio
async def test_the_marker_leaves_out_text_from_steps_already_saved():
    step_end = {"event": "on_chain_end", "name": "agent", "metadata": {"langgraph_node": "agent"}}
    agent = _ScriptedAgent(
        [_text_event("Checking the table."), step_end, _text_event("Found 3")],
        ValueError("x"),
    )

    await _run(agent)

    [marker] = _written(agent)
    assert marker.content.startswith("Found 3\n\n_This response didn't finish.")


@pytest.mark.asyncio
async def test_a_thread_that_cannot_be_loaded_gets_no_half_written_marker():
    agent = _ScriptedAgent([], ValueError("boom"))
    agent.aget_state.side_effect = RuntimeError("pool checkout timed out")

    events = await _run(agent)

    assert any(e["type"] == "error" for e in events)
    agent.aupdate_state.assert_not_awaited()
