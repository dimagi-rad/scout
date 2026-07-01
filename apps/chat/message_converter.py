"""Convert LangChain messages to AI SDK v6 UIMessage format."""

import json
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.chat.constants import SYSTEM_RESUME_MARKER
from apps.chat.stream import _redact_tool_input, _tool_content_to_str


def langchain_messages_to_ui(lc_messages) -> list[dict]:
    """Convert LangChain BaseMessages to AI SDK v6 UIMessage format."""

    visible = [
        m
        for m in lc_messages
        if not (
            isinstance(getattr(m, "content", None), str)
            and m.content.startswith(SYSTEM_RESUME_MARKER)
        )
    ]

    ui_messages: list[dict] = []
    # Collect tool results keyed by tool_call_id for pairing
    tool_results: dict[str, ToolMessage] = {}
    for msg in visible:
        if isinstance(msg, ToolMessage):
            tool_results[msg.tool_call_id] = msg

    for msg in visible:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            ui_messages.append(
                {
                    "id": msg.id or uuid.uuid4().hex,
                    "role": "user",
                    "parts": [{"type": "text", "text": content}],
                }
            )
        elif isinstance(msg, AIMessage):
            parts: list[dict] = []

            # Reasoning (extended-thinking) content. Emitted FIRST and as its
            # own part so the Thinking card survives reload / the
            # post-materialization refetch (arch #246, 13#5). The live stream
            # emits reasoning before text; mirror that ordering here.
            reasoning = ""
            text = ""
            if isinstance(msg.content, str):
                text = msg.content
            elif isinstance(msg.content, list):
                for b in msg.content:
                    if not isinstance(b, dict):
                        text += str(b)
                    elif b.get("type") == "thinking":
                        reasoning += b.get("thinking", "")
                    elif b.get("type") == "text":
                        text += b.get("text", "")
            if reasoning:
                parts.append({"type": "reasoning", "text": reasoning})
            if text:
                parts.append({"type": "text", "text": text})

            # Tool calls
            for tc in getattr(msg, "tool_calls", []) or []:
                tool_part = {
                    "type": f"tool-{tc['name']}",
                    "toolCallId": tc["id"],
                    "toolName": tc["name"],
                    "input": _redact_tool_input(tc.get("args", {})),
                    "state": "output-available",
                }
                # Pair with tool result if available
                tr = tool_results.get(tc["id"])
                output_text = None
                if tr:
                    output_text = _tool_content_to_str(tr)
                    tool_part["output"] = output_text
                tool_part["state"] = "output-available" if tr else "input-available"
                parts.append(tool_part)
                if tc["name"] == "artifact_manager" and output_text:
                    parts.extend(_subagent_trace_parts(output_text, parent_tool_call_id=tc["id"]))

            if parts:
                ui_messages.append(
                    {
                        "id": msg.id or uuid.uuid4().hex,
                        "role": "assistant",
                        "parts": parts,
                    }
                )

    return ui_messages


def _subagent_trace_parts(output_text: str, *, parent_tool_call_id: str) -> list[dict]:
    """Rehydrate persisted Artifact Manager child events into UI data parts."""
    try:
        output = json.loads(output_text)
    except (TypeError, ValueError):
        return []
    if not isinstance(output, dict):
        return []
    trace = output.get("subagent_trace")
    if not isinstance(trace, dict):
        return []
    events = trace.get("events")
    if not isinstance(events, list):
        return []

    parts: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type.startswith("data-subagent-"):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        part = {
            "type": event_type,
            "data": {**data, "parentToolCallId": parent_tool_call_id},
        }
        event_id = event.get("id")
        if isinstance(event_id, str):
            part["id"] = event_id
        parts.append(part)
    return parts
