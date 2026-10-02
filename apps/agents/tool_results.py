"""Shape MCP tool results before they enter agent state.

A ToolMessage is replayed to the model on every later turn and persisted in the
thread checkpoint, so its size is paid again and again. FastMCP renders each
result as indent=2 JSON, and langchain_mcp_adapters keeps a second copy of the
same data as the message ``artifact``, which nothing in Scout reads.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from langchain_core.messages import ToolMessage


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _tool_text(content: Any) -> str | None:
    """The single text payload of a tool result, or None for any other shape."""
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
    ):
        return content[0]["text"]
    return None


def compact_tool_message(message: ToolMessage) -> ToolMessage:
    text = _tool_text(message.content)
    if text is None:
        return message
    try:
        payload = json.loads(text)
    except ValueError:
        return message.model_copy(update={"content": text, "artifact": None})
    return message.model_copy(update={"content": compact_json(payload), "artifact": None})


def compact_tool_results(result: Any, tool_names: Iterable[str]) -> Any:
    """Rewrite a ToolNode result so each named tool's message is compact JSON."""
    if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
        return result
    names = frozenset(tool_names)
    messages = [
        compact_tool_message(message)
        if isinstance(message, ToolMessage) and message.name in names
        else message
        for message in result["messages"]
    ]
    return {**result, "messages": messages}
