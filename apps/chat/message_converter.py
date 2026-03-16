"""Convert LangChain messages to AI SDK v6 UIMessage format."""

import uuid


def langchain_messages_to_ui(lc_messages) -> list[dict]:
    """Convert LangChain BaseMessages to AI SDK v6 UIMessage format."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    ui_messages: list[dict] = []
    # Collect tool results keyed by tool_call_id for pairing
    tool_results: dict[str, ToolMessage] = {}
    for msg in lc_messages:
        if isinstance(msg, ToolMessage):
            tool_results[msg.tool_call_id] = msg

    for msg in lc_messages:
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

            # Text content
            text = ""
            if isinstance(msg.content, str):
                text = msg.content
            elif isinstance(msg.content, list):
                text = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in msg.content
                    if not isinstance(b, dict) or b.get("type") == "text"
                )
            if text:
                parts.append({"type": "text", "text": text})

            # Tool calls
            for tc in getattr(msg, "tool_calls", []) or []:
                tool_part = {
                    "type": f"tool-{tc['name']}",
                    "toolCallId": tc["id"],
                    "toolName": tc["name"],
                    "input": tc.get("args", {}),
                    "state": "output-available",
                }
                # Pair with tool result if available
                tr = tool_results.get(tc["id"])
                if tr:
                    from apps.chat.stream import _tool_content_to_str

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
