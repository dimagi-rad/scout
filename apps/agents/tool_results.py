"""Shape MCP tool results before they enter agent state.

A ToolMessage is replayed to the model on every later turn and persisted in the
thread checkpoint, so its size is paid again and again. FastMCP renders each
result as indent=2 JSON, and langchain_mcp_adapters keeps a second copy of the
same data as the message ``artifact``, which nothing in Scout reads.

Each result is also held to a byte budget. Over budget, the largest lists in the
result are cut from the end and the result says so (``truncated``, a
``truncation`` note, and ``next_offset`` for paged listings), so it stays valid
JSON the agent can act on.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from typing import Any

from langchain_core.messages import ToolMessage

TOOL_RESULT_BUDGET_BYTES = 50_000
# Results the agent reads whole: SQL and semantic rows, already capped by row count,
# and one dataset's or table's members. A normal result must never be cut, so they
# get a ceiling instead, past which the result would crowd out the conversation.
FULL_RESULT_TOOLS = frozenset(
    {"query", "semantic_query", "describe_dataset", "describe_table", "semantic_catalog"}
)
FULL_RESULT_BUDGET_BYTES = 250_000
# Room for the truncation note itself.
_NOTE_RESERVE_BYTES = 1_024


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def result_budget_bytes(tool_name: str | None) -> int:
    return FULL_RESULT_BUDGET_BYTES if tool_name in FULL_RESULT_TOOLS else TOOL_RESULT_BUDGET_BYTES


def _size(value: Any) -> int:
    return len(compact_json(value).encode())


def _dict_lists(node: dict, path: tuple[str, ...] = ()):
    """Every list reachable through dict keys alone.

    Lists inside lists are never entered: cutting inside a SQL row or a dataset entry
    would leave a malformed item rather than fewer whole items.
    """
    for key, value in node.items():
        if isinstance(value, list):
            yield (*path, str(key)), node, key
        elif isinstance(value, dict):
            yield from _dict_lists(value, (*path, str(key)))


def _longest_prefix_within(items: list, max_bytes: int) -> int:
    low, high = 0, len(items)
    while low < high:
        mid = (low + high + 1) // 2
        if _size(items[:mid]) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return low


def _truncation_hint(budget: int, omitted: dict[str, int], next_offset: int | None) -> str:
    left_out = ", ".join(f"{count} from {path}" for path, count in omitted.items())
    hint = f"This result exceeded {budget // 1000} KB, so items were left out ({left_out})."
    if next_offset is not None:
        hint += f" Call again with offset={next_offset} for the next page, or narrow it"
    else:
        hint += " Narrow the request"
    return hint + " with search, filters, fewer columns or a smaller limit."


def fit_to_budget(payload: Any, budget: int) -> Any:
    """``payload`` unchanged when it fits, else a copy cut down to ``budget`` bytes."""
    original_bytes = _size(payload)
    if original_bytes <= budget:
        return payload
    payload = copy.deepcopy(payload) if isinstance(payload, dict) else {"result": payload}
    target = payload["data"] if isinstance(payload.get("data"), dict) else payload
    limit = budget - _NOTE_RESERVE_BYTES
    omitted: dict[str, int] = {}
    total = original_bytes
    while total > limit:
        candidates = [c for c in _dict_lists(target) if c[1][c[2]]]
        if not candidates:
            break
        path, parent, key = max(candidates, key=lambda c: _size(c[1][c[2]]))
        items = parent[key]
        keep = _longest_prefix_within(items, _size(items) - (total - limit))
        parent[key] = items[:keep]
        label = ".".join(path)
        omitted[label] = omitted.get(label, 0) + len(items) - keep
        total = _size(payload)

    next_offset = None
    page_keys = [label for label in omitted if "." not in label]
    if isinstance(target.get("offset"), int) and page_keys:
        next_offset = target["offset"] + len(target[page_keys[0]])
    if total > limit:
        kept = {k: payload[k] for k in ("success", "schema") if target is payload and k in payload}
        target.clear()
        target.update(kept)
        omitted = {"data": 1}
        next_offset = None
    target["truncated"] = True
    if next_offset is not None:
        target["next_offset"] = next_offset
        target["has_more"] = True
    hint = _truncation_hint(budget, omitted, next_offset)
    target["truncation"] = {"budget_bytes": budget, "original_bytes": original_bytes, "hint": hint}
    if isinstance(payload.get("warnings"), list):
        payload["warnings"].append(hint)
    elif "success" in payload:
        payload["warnings"] = [hint]
    return payload


def _cut_text(text: str, budget: int) -> str:
    if len(text.encode()) <= budget:
        return text
    kept = text.encode()[: budget - _NOTE_RESERVE_BYTES].decode(errors="ignore")
    return f"{kept}\n[Truncated: this result exceeded {budget // 1000} KB.]"


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
    budget = result_budget_bytes(message.name)
    try:
        payload = json.loads(text)
    except ValueError:
        return message.model_copy(update={"content": _cut_text(text, budget), "artifact": None})
    content = compact_json(fit_to_budget(payload, budget))
    return message.model_copy(update={"content": content, "artifact": None})


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
