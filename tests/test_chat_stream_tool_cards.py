"""Tests for live/reload parity of tool-output rich cards (arch #246).

The SSE stream must emit per-tool-call events that the frontend can match
against the same tool_call_id it sees on reload (the LLM ``toolu_…`` id), with
the real tool input and parse-safe JSON output, so progress / Stop / failure
cards and rich rendering work LIVE -- not only after a page reload.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from langchain_core.messages import ToolMessage

from apps.chat import stream


def _parse_sse(chunks: list[str]) -> list[dict]:
    """Parse a list of ``data: {json}\\n\\n`` strings into chunk dicts."""
    out = []
    for c in chunks:
        line = c.removeprefix("data: ").strip()
        out.append(json.loads(line))
    return out


class _ToolEventAgent:
    """Stands in for the LangGraph agent, replaying a fixed event sequence."""

    def __init__(self, events: list[dict]):
        self._events = events

    def astream_events(self, input_state, *, config, version):
        events = self._events

        async def _gen():
            for ev in events:
                yield ev

        return _gen()


async def _run(events: list[dict]) -> list[dict]:
    agent = _ToolEventAgent(events)
    chunks = [
        c
        async for c in stream.langgraph_to_ui_stream(
            agent, {}, {"configurable": {"thread_id": "t1"}}
        )
    ]
    return _parse_sse(chunks)


# --- toolCallId is the LLM toolu_ id, not the LangGraph run_id (06#3) --------


@pytest.mark.asyncio
async def test_tool_output_uses_llm_tool_call_id_not_run_id():
    """The live stream must emit the LLM ``toolu_…`` id so per-card progress /
    Stop / failure render live (ThreadJob.tool_call_id is the toolu_ id)."""
    tm = ToolMessage(
        content=json.dumps({"success": True, "data": {"status": "started"}}),
        tool_call_id="toolu_REAL123",
    )
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run-uuid-abc",
            "name": "run_materialization",
            "data": {"input": {"workspace_id": "ws", "tool_call_id": "toolu_REAL123"}},
        },
        {
            "event": "on_tool_end",
            "run_id": "run-uuid-abc",
            "name": "run_materialization",
            "data": {"output": tm},
        },
    ]
    chunks = await _run(events)

    tool_inputs = [c for c in chunks if c["type"] == "tool-input-available"]
    tool_outputs = [c for c in chunks if c["type"] == "tool-output-available"]

    assert tool_inputs, "expected a tool-input-available chunk"
    assert tool_outputs, "expected a tool-output-available chunk"

    # The id must be the LLM toolu_ id, never the LangGraph run_id.
    for c in tool_inputs + tool_outputs:
        assert c["toolCallId"] == "toolu_REAL123"
        assert c["toolCallId"] != "run-uuid-abc"

    # start and end must share the same id so AI SDK pairs them into one part.
    assert tool_inputs[0]["toolCallId"] == tool_outputs[0]["toolCallId"]


# --- on_tool_start emits a loading state with the real input (13#4) ----------


@pytest.mark.asyncio
async def test_on_tool_start_emits_real_input_and_loading_state():
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run-1",
            "name": "query",
            "data": {
                "input": {
                    "sql": "SELECT 1",
                    "workspace_id": "ws-secret",
                    "user_id": "u",
                    "thread_id": "t",
                    "tool_call_id": "toolu_Q",
                }
            },
        },
    ]
    chunks = await _run(events)
    starts = [c for c in chunks if c["type"] == "tool-input-available"]
    assert starts, "on_tool_start should emit a tool-input-available chunk"
    start = starts[0]
    assert start["toolCallId"] == "toolu_Q"
    assert start["toolName"] == "query"
    # Real input is surfaced...
    assert start["input"].get("sql") == "SELECT 1"
    # ...but injected/hidden context params are stripped.
    assert "workspace_id" not in start["input"]
    assert "user_id" not in start["input"]
    assert "thread_id" not in start["input"]
    assert "tool_call_id" not in start["input"]


# --- output JSON stays parse-safe; not double-pretty-printed (13#4/13#7) -----


@pytest.mark.asyncio
async def test_tool_output_is_parse_safe_json_not_truncated_mid_token():
    """A large query result must remain valid JSON so the rich card parses it
    LIVE. The old 2000-char hard truncation cut JSON mid-token."""
    rows = [[i, f"name-{i}", "x" * 50] for i in range(200)]
    payload = {
        "success": True,
        "data": {"columns": ["id", "name", "blob"], "rows": rows, "row_count": 200},
    }
    tm = ToolMessage(content=json.dumps(payload), tool_call_id="toolu_BIG")
    events = [
        {
            "event": "on_tool_end",
            "run_id": "r",
            "name": "query",
            "data": {"output": tm},
        },
    ]
    chunks = await _run(events)
    outs = [c for c in chunks if c["type"] == "tool-output-available"]
    assert outs
    out = outs[0]["output"]
    # The output must parse as JSON (no mid-token truncation).
    parsed = json.loads(out)
    assert parsed["data"]["row_count"] == 200
    assert len(parsed["data"]["rows"]) == 200


@pytest.mark.asyncio
async def test_tool_output_not_double_indented():
    """Output must be emitted as compact JSON, not re-pretty-printed with
    indent=2 (which bloats payloads and breaks the 2000-char heuristics)."""
    payload = {"success": True, "data": {"k": "v"}}
    tm = ToolMessage(content=json.dumps(payload), tool_call_id="toolu_C")
    events = [
        {"event": "on_tool_end", "run_id": "r", "name": "query", "data": {"output": tm}},
    ]
    chunks = await _run(events)
    out = next(c for c in chunks if c["type"] == "tool-output-available")["output"]
    # Compact: round-trips and has no indentation newlines from indent=2.
    assert json.loads(out) == payload
    assert '\n  "' not in out


# --- a non-serializable injected param must not crash the stream (SCOUT-DJANGO-1V)


def _make_tool_runtime():
    """Build a real langgraph ToolRuntime, like ToolNode injects into MCP tools.

    langchain_mcp_adapters gives every MCP tool a ``runtime`` param and
    langgraph's ToolNode injects a ToolRuntime object into it, which then shows
    up in the on_tool_start event's input. It is NOT JSON serializable. Fall
    back to a plain non-serializable object if the internal signature shifts.
    """
    try:
        from langgraph.prebuilt.tool_node import ToolRuntime

        return ToolRuntime(
            state={},
            context=None,
            config={},
            stream_writer=None,
            tool_call_id="toolu_RT",
            store=None,
        )
    except Exception:  # pragma: no cover - signature drift fallback

        class _Opaque:
            pass

        return _Opaque()


@pytest.mark.asyncio
async def test_tool_runtime_in_input_does_not_crash_stream():
    """Regression for SCOUT-DJANGO-1V: a ToolRuntime injected into the tool
    input must not crash the SSE stream. The tool-input card must still render
    with the real input, and the non-serializable ``runtime`` must not leak."""
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run-1",
            "name": "query",
            "data": {
                "input": {
                    "sql": "SELECT 1",
                    "workspace_id": "ws-secret",
                    "tool_call_id": "toolu_RT",
                    "runtime": _make_tool_runtime(),
                }
            },
        },
    ]
    chunks = await _run(events)

    # The stream must not have degraded into the generic error text.
    error_deltas = [
        c
        for c in chunks
        if c.get("type") == "text-delta" and "An error occurred" in c.get("delta", "")
    ]
    assert not error_deltas, "stream crashed and emitted the generic error text"

    starts = [c for c in chunks if c["type"] == "tool-input-available"]
    assert starts, "on_tool_start should still emit a tool-input-available chunk"
    start = starts[0]
    assert start["toolCallId"] == "toolu_RT"
    assert start["input"].get("sql") == "SELECT 1"
    # The injected, non-serializable runtime must not surface in the card.
    assert "runtime" not in start["input"]
    assert "workspace_id" not in start["input"]


class _StallingStream:
    """An event stream that never yields — models a hung LLM/tool call."""

    def __init__(self):
        self.aclosed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()  # hang forever

    async def aclose(self):
        self.aclosed = True


class _StallingAgent:
    def __init__(self, stream_obj):
        self._stream = stream_obj

    def astream_events(self, input_state, *, config, version):
        return self._stream


@pytest.mark.asyncio
async def test_stream_times_out_on_stalled_event_and_closes_generator():
    """arch #255, 02#8: a stalled call that emits no events must still trip the
    deadline (the wait for the NEXT event is bounded), and the abandoned
    generator must be aclose()d so the in-flight ainvoke/Anthropic call is
    cancelled rather than leaking until GC."""
    stalling = _StallingStream()
    agent = _StallingAgent(stalling)
    with patch.object(stream, "AGENT_TIMEOUT_SECONDS", 0.2):
        chunks = [
            c
            async for c in stream.langgraph_to_ui_stream(
                agent, {}, {"configurable": {"thread_id": "t1"}}
            )
        ]
    parsed = _parse_sse(chunks)
    assert any(
        c.get("type") == "error" and "timed out" in c.get("errorText", "").lower() for c in parsed
    ), "a stalled stream must emit a timeout error chunk"
    assert stalling.aclosed is True, "the abandoned event generator must be closed on timeout"


def test_sse_survives_non_serializable_values():
    """``_sse`` is the last line of defence: no single non-serializable value
    may ever crash the whole stream, so it falls back to ``default=str``."""
    chunk = {"type": "tool-input-available", "input": {"runtime": _make_tool_runtime()}}
    out = stream._sse(chunk)
    assert out.startswith("data: ")
    assert out.endswith("\n\n")
    # Still valid JSON; the opaque value is coerced to a string rather than raising.
    parsed = json.loads(out.removeprefix("data: ").strip())
    assert parsed["type"] == "tool-input-available"
