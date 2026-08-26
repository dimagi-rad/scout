"""Reusable nested-event forwarding for parent-facing subagent tools.

Generalizes the streaming glue first built for the Artifact Manager: LangGraph
astream_events from a nested graph are re-emitted into the chat SSE stream as
``data-subagent-*`` envelopes tied to the parent tool call, and mirrored into a
bounded, JSON-safe trace persisted on the parent tool result.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from apps.agents.subagents.events import emit_subagent_event

TRACE_MAX_EVENTS = 200
MESSAGE_MAX_CHARS = 40_000


class NestedEventForwarder:
    """Forwards one nested graph run's events for a named subagent."""

    def __init__(self, subagent_name: str, parent_tool_call_id: str) -> None:
        self.subagent_name = subagent_name
        self.parent_tool_call_id = parent_tool_call_id
        self._run_to_tool_call_id: dict[str, str] = {}
        self._pending_tool_starts: dict[str, dict[str, Any]] = {}
        self._message_buffers: dict[tuple[str, str], str] = {}
        self._trace_events: list[dict[str, Any]] = []
        self._trace_index: dict[tuple[str, str], int] = {}

    # -- public API ---------------------------------------------------------

    async def status(self, *, phase: str, message: str, **extra: Any) -> None:
        data: dict[str, Any] = {
            "parentToolCallId": self.parent_tool_call_id,
            "subagentName": self.subagent_name,
            "phase": phase,
            "message": message,
        }
        data.update({k: v for k, v in extra.items() if v is not None})
        await self._emit(
            {
                "type": "data-subagent-status",
                "id": f"{self.subagent_name}:{self.parent_tool_call_id}:status",
                "data": data,
            }
        )

    async def forward(self, event: dict[str, Any]) -> None:
        event_type = event.get("event")
        if event_type == "on_tool_start":
            await self._tool_start(event)
        elif event_type == "on_tool_end":
            await self._tool_end(event)
        elif event_type == "on_chat_model_stream":
            await self._chat_stream(event)
        elif event_type in {"on_tool_error", "on_chain_error"}:
            await self._emit(
                {
                    "type": "data-subagent-error",
                    "id": f"{self.subagent_name}:{event.get('run_id') or uuid.uuid4().hex}:error",
                    "data": {
                        "parentToolCallId": self.parent_tool_call_id,
                        "subagentName": self.subagent_name,
                        "message": str(event.get("data", {}).get("error") or "Subagent error"),
                    },
                }
            )

    def trace(self) -> dict[str, Any]:
        return {"subagentName": self.subagent_name, "events": self._trace_events}

    # -- event handlers -----------------------------------------------------

    async def _tool_start(self, event: dict[str, Any]) -> None:
        from apps.chat.stream import _redact_tool_input

        raw_input = event.get("data", {}).get("input")
        run_id = str(event.get("run_id") or "")
        tool_call_id = raw_input.get("tool_call_id") if isinstance(raw_input, dict) else None
        if not tool_call_id:
            if run_id:
                # Local tools carry no tool_call_id in their input; buffer the
                # start until the ToolMessage on_tool_end supplies the id.
                self._pending_tool_starts[run_id] = {
                    "toolName": event.get("name", "unknown"),
                    "input": _redact_tool_input(raw_input),
                }
                return
            tool_call_id = uuid.uuid4().hex
        if run_id:
            self._run_to_tool_call_id[run_id] = str(tool_call_id)
        await self._emit_tool_input(
            self._child_id(str(tool_call_id)),
            event.get("name", "unknown"),
            _redact_tool_input(raw_input),
        )

    async def _tool_end(self, event: dict[str, Any]) -> None:
        from apps.chat.stream import _tool_content_to_str, _truncate_tool_output

        tool_output = event.get("data", {}).get("output")
        if not tool_output:
            return
        run_id = str(event.get("run_id") or "")
        output_tool_call_id = getattr(tool_output, "tool_call_id", None)
        started_tool_call_id = self._run_to_tool_call_id.get(run_id)
        raw_id = output_tool_call_id or started_tool_call_id or run_id or uuid.uuid4().hex
        child_id = self._child_id(str(raw_id))
        pending_start = self._pending_tool_starts.pop(run_id, None)
        if not started_tool_call_id or (
            output_tool_call_id and output_tool_call_id != started_tool_call_id
        ):
            await self._emit_tool_input(
                child_id,
                event.get("name", "unknown"),
                (pending_start or {}).get("input", {}),
            )
        await self._emit(
            {
                "type": "data-subagent-tool-output",
                "id": f"{child_id}:output",
                "data": {
                    "parentToolCallId": self.parent_tool_call_id,
                    "subagentName": self.subagent_name,
                    "toolCallId": child_id,
                    "toolName": event.get("name", "unknown"),
                    "output": _truncate_tool_output(_tool_content_to_str(tool_output)),
                },
            }
        )

    async def _chat_stream(self, event: dict[str, Any]) -> None:
        chunk = event.get("data", {}).get("chunk")
        if not chunk or not hasattr(chunk, "content") or not chunk.content:
            return
        run_id = str(event.get("run_id") or uuid.uuid4().hex)
        for kind, text in _extract_chunk_texts(chunk.content):
            buffer_key = (run_id, kind)
            next_text = f"{self._message_buffers.get(buffer_key, '')}{text}"
            if len(next_text) > MESSAGE_MAX_CHARS:
                next_text = next_text[-MESSAGE_MAX_CHARS:]
            self._message_buffers[buffer_key] = next_text
            await self._emit(
                {
                    "type": f"data-subagent-{kind}",
                    "id": f"{self.subagent_name}:{run_id}:{kind}",
                    "data": {
                        "parentToolCallId": self.parent_tool_call_id,
                        "subagentName": self.subagent_name,
                        "kind": kind,
                        "text": next_text,
                        "delta": text,
                    },
                }
            )

    # -- internals ----------------------------------------------------------

    async def _emit_tool_input(self, child_id: str, tool_name: str, tool_input: Any) -> None:
        await self._emit(
            {
                "type": "data-subagent-tool-input",
                "id": f"{child_id}:input",
                "data": {
                    "parentToolCallId": self.parent_tool_call_id,
                    "subagentName": self.subagent_name,
                    "toolCallId": child_id,
                    "toolName": tool_name,
                    "input": tool_input,
                },
            }
        )

    async def _emit(self, event: dict[str, Any]) -> None:
        self._trace_add(event)
        await emit_subagent_event(event)

    def _trace_add(self, event: dict[str, Any]) -> None:
        clean = json.loads(json.dumps(event, default=str))
        event_type = clean.get("type")
        event_id = clean.get("id")
        if isinstance(event_type, str) and isinstance(event_id, str):
            key = (event_type, event_id)
            existing = self._trace_index.get(key)
            if existing is not None:
                self._trace_events[existing] = clean
                return
            self._trace_index[key] = len(self._trace_events)
        if len(self._trace_events) >= TRACE_MAX_EVENTS:
            return
        self._trace_events.append(clean)

    def _child_id(self, raw_id: str) -> str:
        prefix = f"{self.subagent_name}:"
        return raw_id if raw_id.startswith(prefix) else f"{prefix}{raw_id}"


def _extract_chunk_texts(content: Any) -> list[tuple[str, str]]:
    if isinstance(content, str):
        return [("text", content)] if content else []
    result: list[tuple[str, str]] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    result.append(("text", block["text"]))
                elif block.get("type") == "thinking" and block.get("thinking"):
                    result.append(("reasoning", block["thinking"]))
            elif hasattr(block, "text") and block.text:
                result.append(("text", block.text))
    return result
