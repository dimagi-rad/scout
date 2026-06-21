"""Tests for live/reload parity of tool-output rich cards (arch #246).

The SSE stream must emit per-tool-call events that the frontend can match
against the same tool_call_id it sees on reload (the LLM ``toolu_…`` id), with
the real tool input and parse-safe JSON output, so progress / Stop / failure
cards and rich rendering work LIVE -- not only after a page reload.
"""

import json

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


@pytest.mark.asyncio
async def test_local_tool_output_id_matches_its_start_id():
    """Local tools (create_artifact, define_crossopp_measure) get NO injected
    tool_call_id, so on_tool_start emits the run_id. on_tool_end MUST reuse that
    same id rather than the ToolMessage's (different) toolu_ id — otherwise the
    AI SDK client errors 'No tool invocation found for tool call ID toolu_...'
    and the whole chat turn fails.
    """
    tm = ToolMessage(
        content=json.dumps({"status": "committed", "measure": "x"}),
        tool_call_id="toolu_LOCAL999",
    )
    events = [
        {
            "event": "on_tool_start",
            "run_id": "run-local-1",
            "name": "define_crossopp_measure",
            # No tool_call_id in the input — local tools aren't injected with it.
            "data": {"input": {"name": "x", "description": "y", "kind": "numeric"}},
        },
        {
            "event": "on_tool_end",
            "run_id": "run-local-1",
            "name": "define_crossopp_measure",
            "data": {"output": tm},
        },
    ]
    chunks = await _run(events)
    ins = [c for c in chunks if c["type"] == "tool-input-available"]
    outs = [c for c in chunks if c["type"] == "tool-output-available"]
    assert ins and outs
    # The output's id must match an emitted input id, or the client can't pair them.
    input_ids = {c["toolCallId"] for c in ins}
    assert outs[0]["toolCallId"] in input_ids, (
        f"output id {outs[0]['toolCallId']} has no matching input id {input_ids}"
    )
