"""Tests for two truthful-failure fixes in the chat SSE stream (arch #256).

06#1 — the panic-loop escalation node returns a fixed ``AIMessage`` (no
``on_chat_model_stream`` event), so the escalation message was never turned
into a live text-delta and only appeared on reload. ``langgraph_to_ui_stream``
must translate the escalate node's output into a streamed text-delta.

06#4 — on any non-transient exception the stream emitted an apology as plain
message text plus a normal ``finish`` with
``finishReason 'stop'`` and NO error chunk, so the frontend ``useChat`` error
state never fired and a failed run was indistinguishable from success. The
stream must emit a native AI SDK ``{"type":"error"}`` chunk so the failure
surfaces.
"""

import json

import pytest
from langchain_core.messages import AIMessage

from apps.agents.graph.base import ESCALATION_MESSAGE
from apps.chat import stream


def _parse_sse(chunks: list[str]) -> list[dict]:
    out = []
    for c in chunks:
        line = c.removeprefix("data: ").strip()
        out.append(json.loads(line))
    return out


class _EventAgent:
    """Replays a fixed event sequence as the LangGraph agent would."""

    def __init__(self, events: list[dict]):
        self._events = events

    def astream_events(self, input_state, *, config, version):
        events = self._events

        async def _gen():
            for ev in events:
                yield ev

        return _gen()


class _RaisingAgent:
    """Its event stream raises immediately (mirrors a mid-run failure)."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def astream_events(self, input_state, *, config, version):
        exc = self._exc

        async def _gen():
            if False:  # async generator that yields nothing, then raises
                yield
            raise exc

        return _gen()


async def _run_events(events: list[dict]) -> list[dict]:
    agent = _EventAgent(events)
    return _parse_sse(
        [
            c
            async for c in stream.langgraph_to_ui_stream(
                agent, {}, {"configurable": {"thread_id": "t1"}}
            )
        ]
    )


async def _run_raising(exc: BaseException) -> list[dict]:
    agent = _RaisingAgent(exc)
    return _parse_sse(
        [
            c
            async for c in stream.langgraph_to_ui_stream(
                agent, {}, {"configurable": {"thread_id": "t1"}}
            )
        ]
    )


# --- 06#1: escalation message is streamed live -----------------------------


@pytest.mark.asyncio
async def test_escalation_node_output_is_streamed_as_text_delta():
    """The terminal ``escalate`` node returns a hardcoded AIMessage. The stream
    must surface its content as a live text-delta so the user sees the recovery
    affordance during the turn, not only after reload."""
    events = [
        {
            "event": "on_chain_end",
            "name": "escalate",
            "data": {"output": {"messages": [AIMessage(content=ESCALATION_MESSAGE)]}},
        }
    ]
    chunks = await _run_events(events)

    deltas = [c for c in chunks if c.get("type") == "text-delta"]
    assert deltas, "escalation produced no text-delta — user sees nothing live"
    streamed = "".join(c["delta"] for c in deltas)
    assert ESCALATION_MESSAGE in streamed

    # The text part is opened and closed so the AI SDK renders it cleanly.
    types = [c["type"] for c in chunks]
    assert "text-start" in types
    assert "text-end" in types
    # Stream still finishes normally (the escalation is a clean end-of-turn).
    assert any(c["type"] == "finish" for c in chunks)


@pytest.mark.asyncio
async def test_non_escalate_chain_end_is_ignored():
    """Only the ``escalate`` node's output is surfaced — a normal node's
    on_chain_end carrying messages must NOT be double-emitted as text (the LLM
    text already arrived via on_chat_model_stream)."""
    events = [
        {
            "event": "on_chain_end",
            "name": "agent",
            "data": {"output": {"messages": [AIMessage(content="regular answer")]}},
        }
    ]
    chunks = await _run_events(events)
    deltas = [c for c in chunks if c.get("type") == "text-delta"]
    assert not deltas


# --- 06#4: failures emit an error chunk, not a silent successful finish -----


@pytest.mark.asyncio
async def test_upstream_timeout_error_emits_generic_error_chunk():
    """A TimeoutError from the upstream graph is treated as a normal exception.

    The live chat SSE bridge does not impose its own wall-clock timeout.
    """
    chunks = await _run_raising(TimeoutError("agent exceeded timeout"))

    errors = [c for c in chunks if c.get("type") == "error"]
    assert errors, "exception emitted no error chunk — failure looks like success"
    assert "errorText" in errors[0]
    assert "timed out" not in errors[0]["errorText"]
    # The error text carries a correlation ref the user can quote.
    assert "Ref:" in errors[0]["errorText"]


@pytest.mark.asyncio
async def test_generic_exception_emits_error_chunk():
    chunks = await _run_raising(ValueError("boom"))
    errors = [c for c in chunks if c.get("type") == "error"]
    assert errors, "generic exception emitted no error chunk"
    assert "Ref:" in errors[0]["errorText"]


@pytest.mark.asyncio
async def test_transient_overload_does_not_emit_error_chunk():
    """The transient-overload path is auto-retried by the frontend and must NOT
    emit a hard error chunk (which would surface a dead-end error state)."""
    import httpx
    from anthropic import APIStatusError

    body = {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "Overloaded"},
    }
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = APIStatusError(f"{body}", response=httpx.Response(200, request=request), body=body)

    chunks = await _run_raising(exc)
    assert not [c for c in chunks if c.get("type") == "error"]
    # Still signals the retryable condition the frontend keys auto-retry off of.
    assert any(c.get("type") == "data-chat-status" for c in chunks)
