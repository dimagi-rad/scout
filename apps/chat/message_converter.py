"""Convert LangChain messages to AI SDK v6 UIMessage format."""

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

            # Reasoning emitted FIRST as its own part so the Thinking card survives
            # reload / post-materialization refetch (arch #246, 13#5); mirrors live-stream order.
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

            for tc in getattr(msg, "tool_calls", []) or []:
                tool_part = {
                    "type": f"tool-{tc['name']}",
                    "toolCallId": tc["id"],
                    "toolName": tc["name"],
                    "input": _redact_tool_input(tc.get("args", {})),
                    "state": "output-available",
                }
                tr = tool_results.get(tc["id"])
                if tr:
                    tool_part["output"] = _tool_content_to_str(tr)
                tool_part["state"] = "output-available" if tr else "input-available"
                parts.append(tool_part)

            if parts:
                ui_messages.append(
                    {
                        "id": msg.id or uuid.uuid4().hex,
                        "role": "assistant",
                        "parts": parts,
                    }
                )

    return ui_messages
