"""Run policy for the main agent graph: when a turn ends early, and with what.

Tool-error escalation (the panic-loop breaker and the workspace-access denial),
the fixed messages those terminal nodes emit, and the fallback task synthesized
for an ``artifact_manager`` call that arrives without one.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.common.error_codes import ErrorCode

# Panic-loop circuit breaker: if the last N tool messages all carry one of these
# error codes, route to the escalation node so the turn ends with an explicit ask
# instead of burning the recursion budget. These are bare ``error.code`` values
# from the MCP envelope, matched against the parsed JSON field — NOT a substring
# search: substring-matching ``'"code": "NOT_FOUND"'`` only worked under
# FastMCP's indent=2 and would silently break under compact separators (06#1).
# Single source of truth shared with base_system.py's "When the Schema is Broken".
ESCALATION_ERROR_CODES = frozenset(
    {
        ErrorCode.NOT_FOUND,
        ErrorCode.VALIDATION_ERROR,
        # semantic_query and the SQL tools reported these as VALIDATION_ERROR
        # until #251, so they keep escalating as they did.
        ErrorCode.DATA_NOT_LOADED,
        ErrorCode.SEMANTIC_MODEL_UNAVAILABLE,
    }
)
ESCALATION_TRIGGER_COUNT = 3
ARTIFACT_MANAGER_SYNTHETIC_TASK_MAX_CHARS = 2_000


def _tool_message_error(content: Any) -> dict | None:
    """Extract the MCP envelope ``error`` object from a ToolMessage's content.

    Content may be a JSON string, a list of content blocks (the
    langchain_mcp_adapters shape), or already-parsed structures. Reads the
    structured ``error`` rather than a whitespace-sensitive substring (06#1).
    Returns None when the content isn't a recognizable error envelope.
    """
    if isinstance(content, list):
        for block in content:
            text = None
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
            elif isinstance(block, str):
                text = block
            if text:
                error = _tool_message_error(text)
                if error is not None:
                    return error
        return None
    if isinstance(content, dict):
        envelope = content
    elif isinstance(content, str):
        try:
            envelope = json.loads(content)
        except (ValueError, TypeError):
            return None
    else:
        return None
    if not isinstance(envelope, dict) or envelope.get("success") is not False:
        return None
    error = envelope.get("error")
    return error if isinstance(error, dict) else None


def _schema_escalation_message(messages: list, *, write_capable: bool, interactive: bool) -> str:
    """The escalation text for the streak ``_should_escalate`` matched.

    Only a streak whose every result is a missing data model gets the rebuild text:
    a reload fixes anything else, and parallel results arrive in no fixed order.
    """
    codes = {_tool_message_error_code(m.content) for m in _escalation_streak(messages)}
    if codes == {ErrorCode.SEMANTIC_MODEL_UNAVAILABLE}:
        return (
            SEMANTIC_ESCALATION_MESSAGE if write_capable else READ_ONLY_SEMANTIC_ESCALATION_MESSAGE
        )
    if not write_capable:
        return READ_ONLY_ESCALATION_MESSAGE
    if not interactive:
        return HEADLESS_ESCALATION_MESSAGE
    return ESCALATION_MESSAGE


def _tool_message_error_code(content: Any) -> str | None:
    """The envelope ``error.code`` of a ToolMessage's content, if any."""
    code = (_tool_message_error(content) or {}).get("code")
    return code if isinstance(code, str) else None


def _workspace_access_denial(messages: list) -> str | None:
    """The authorizer's message when the latest tool round denied workspace access.

    The denial holds for every remaining tool call this turn, so the graph ends
    the turn on it with the remedy instead of letting the agent retry. The whole
    trailing run of tool results is one round (parallel calls), and a successful
    sibling in that round must not hide the denial.
    """
    batch = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        batch.append(message)
    error = next(
        (
            e
            for e in (_tool_message_error(m.content) for m in batch)
            if e and e.get("code") == ErrorCode.WORKSPACE_ACCESS_DENIED
        ),
        None,
    )
    if error is None:
        return None
    message = error.get("message")
    return message if isinstance(message, str) and message else ACCESS_DENIED_MESSAGE


ACCESS_DENIED_MESSAGE = "I can no longer read this workspace's data."

ESCALATION_MESSAGE = (
    "I've encountered repeated schema errors — the tables I expected to "
    "find aren't queryable. The data may need to be re-materialized. "
    "Would you like me to run materialization?"
)

# A headless run has nobody to answer the interactive question.
HEADLESS_ESCALATION_MESSAGE = (
    "I've encountered repeated schema errors — the tables I expected to "
    "find aren't queryable. The data may need to be re-materialized before "
    "this run can complete."
)

READ_ONLY_ESCALATION_MESSAGE = (
    "I've encountered repeated schema errors — the tables I expected to "
    "find aren't queryable. The data may need to be refreshed, which a "
    "workspace member with write access can do."
)

# A missing data model is fixed by rebuilding it, not by reloading the data.
SEMANTIC_ESCALATION_MESSAGE = (
    "I've encountered repeated errors — this workspace's data model (its semantic "
    "datasets) isn't available, so its data can't be queried yet. The data model "
    "needs to be rebuilt; that does not reload any data."
)

READ_ONLY_SEMANTIC_ESCALATION_MESSAGE = (
    "I've encountered repeated errors — this workspace's data model (its semantic "
    "datasets) isn't available, so its data can't be queried yet. A workspace member "
    "with write access can rebuild the data model; that does not reload any data."
)

# Marks the escalation node's message so headless callers (recipe runs) can tell
# an ended-on-escalation turn from a real answer without matching its prose.
ESCALATION_METADATA_KEY = "scout_escalation"

# A refusal, a max_tokens cut-off, or a turn with only thinking would otherwise
# end the chat with a blank reply and a clean finish.
MODEL_STOPPED_MESSAGES = {
    "refusal": "The model declined to answer this request. Try rephrasing it.",
    "max_tokens": "The model stopped before finishing its answer; try again.",
    "empty": "The model stopped before answering; try again.",
}


def _escalation_streak(messages: list) -> list[ToolMessage]:
    """The newest ESCALATION_TRIGGER_COUNT tool results of the turn, across rounds."""
    streak: list[ToolMessage] = []
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            streak.append(msg)
            if len(streak) >= ESCALATION_TRIGGER_COUNT:
                break
        elif isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            continue
        else:
            break
    return streak


def _should_escalate(messages: list) -> bool:
    """Detect a panic loop: last N trailing tool messages all returned an
    escalation error code. A successful tool call in between resets the streak.
    Matches the structured ``error.code`` (06#1), not a substring.
    """
    streak = _escalation_streak(messages)
    if len(streak) < ESCALATION_TRIGGER_COUNT:
        return False

    for tm in streak:
        code = _tool_message_error_code(tm.content)
        if code not in ESCALATION_ERROR_CODES:
            return False
    return True


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _artifact_manager_task_is_missing(args: Any) -> bool:
    if not isinstance(args, dict):
        return True
    task = args.get("task")
    return not isinstance(task, str) or not task.strip()


def _latest_human_text(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            text = _message_text(msg).strip()
            if text:
                return text
    return ""


def _synthesize_artifact_manager_task(messages: list[Any]) -> str:
    """Build a bounded fallback task when the LLM emits an empty tool call.

    Anthropic can still produce an empty argument object despite a required
    schema. For artifact work, the user's latest request is enough context: the
    Artifact Manager subagent owns data discovery, semantic query verification,
    graph construction, and validation.
    """

    user_request = _latest_human_text(messages)
    if len(user_request) > ARTIFACT_MANAGER_SYNTHETIC_TASK_MAX_CHARS:
        user_request = user_request[:ARTIFACT_MANAGER_SYNTHETIC_TASK_MAX_CHARS].rstrip()
        user_request += "..."
    if not user_request:
        user_request = "Create or update the semantic story artifact requested in this thread."

    return (
        "Create or update a semantic story artifact for the user's latest request. "
        "Treat this as a complete delegated artifact task. Do your own dataset "
        "discovery and semantic-query verification inside the Artifact Manager; "
        "use live semantic data, hidden semantic_query blocks, validated graph/table/stat "
        "bindings, and publish only after artifact_write validation succeeds.\n\n"
        f"User request:\n{user_request}"
    )
