"""
Agent state definition for the Scout data agent platform.

Defines the AgentState TypedDict that flows through the LangGraph conversation
graph. All fields are JSON-serializable for Postgres checkpoint persistence.
"""

from typing import Annotated

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

DEFAULT_MAX_MESSAGES = 20


def prune_messages(
    messages: list[BaseMessage],
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> list[BaseMessage]:
    """Keep system messages plus the most recent ``max_messages`` conversation
    messages, without breaking tool call/response pairs.
    """
    if len(messages) <= max_messages:
        return messages

    system_messages: list[BaseMessage] = []
    conversation_messages: list[BaseMessage] = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            system_messages.append(msg)
        else:
            conversation_messages.append(msg)

    if len(conversation_messages) <= max_messages:
        return system_messages + conversation_messages

    pruned_conversation = conversation_messages[-max_messages:]

    # Drop leading ToolMessages orphaned from their (now-pruned) parent AIMessage.
    while pruned_conversation and hasattr(pruned_conversation[0], "tool_call_id"):
        pruned_conversation = pruned_conversation[1:]

    return system_messages + pruned_conversation


def all_tool_calls(message: AIMessage) -> list[dict]:
    """Every tool call the message's ``tool_use`` blocks carry, parsed or not.

    A ``tool_use`` whose partial JSON can't be parsed lands in
    ``invalid_tool_calls``, yet langchain-anthropic still replays its block, so
    it needs a ``tool_result`` like any other call or the thread 400s for good.
    """
    return [*(message.tool_calls or []), *(message.invalid_tool_calls or [])]


TRUNCATED_TOOL_CALL_MESSAGE = (
    "Not run: your reply hit the output token limit before this tool call's "
    "arguments were complete. Retry with a smaller input, for example by splitting "
    "the change into several calls."
)

# Marks the rejection ToolMessage so code can recognise it without matching the
# model-facing prose, which is free to change.
REJECTED_TOOL_CALL_KWARG = "scout_rejected_tool_call"


def is_rejected_tool_result(message: BaseMessage) -> bool:
    """Whether this is the error result for a tool call the graph refused to run."""
    return isinstance(message, ToolMessage) and bool(
        message.additional_kwargs.get(REJECTED_TOOL_CALL_KWARG)
    )


def truncated_tool_calls(message: BaseMessage) -> list[dict]:
    """Tool calls from a turn cut off by ``max_tokens``.

    langchain parses partial tool JSON leniently, so a call cut mid-argument
    still arrives in ``tool_calls`` with plausible but truncated args (a SQL
    string ending at ``SELECT``, half an artifact). Running it would act on
    input the model never finished writing.
    """
    if not isinstance(message, AIMessage):
        return []
    if message.response_metadata.get("stop_reason") != "max_tokens":
        return []
    return all_tool_calls(message)


def unfinished_turn_reason(message: BaseMessage) -> str | None:
    """Why the latest turn left the user without an answer, or None if it didn't.

    Covers a final (tool-call-free) model turn that was refused, cut off at
    ``max_tokens`` or has no text, and a turn that ended on a rejected
    truncated tool call after the retries ran out.
    """
    if isinstance(message, ToolMessage):
        return "max_tokens" if is_rejected_tool_result(message) else None
    if not isinstance(message, AIMessage) or message.tool_calls:
        return None
    stop_reason = message.response_metadata.get("stop_reason")
    if stop_reason in ("refusal", "max_tokens"):
        return stop_reason
    if not message.text.strip():
        return "empty"
    return None


def model_cut_off_reason(messages: list[BaseMessage]) -> str | None:
    """``refusal`` or ``max_tokens`` when the model stopped a subagent run itself.

    Subagents use this rather than ``unfinished_turn_reason``: they report
    their outcome from tool results, so a text-free final turn can still be a
    success. The subagent callers rebuild ``messages`` from stream events, where
    the input HumanMessage can land last, so the run's final model or tool
    message is looked up rather than taken from the end.
    """
    final = next((m for m in reversed(messages) if isinstance(m, AIMessage | ToolMessage)), None)
    reason = unfinished_turn_reason(final) if final is not None else None
    return reason if reason in ("refusal", "max_tokens") else None


UNFINISHED_TURN_DESCRIPTIONS = {
    "refusal": "the model declined the request",
    "max_tokens": "the model ran out of output tokens",
    "empty": "the model returned no answer",
}


TRUNCATED_TOOL_CALLS_NODE = "truncated_tool_calls"

# A request that reliably overruns max_tokens (say, one huge artifact) would
# otherwise retry at 16k output tokens a pass until the recursion limit.
MAX_TRUNCATED_RETRIES = 2


def truncated_retries_exhausted(messages: list[BaseMessage]) -> bool:
    """Whether this turn (since the last human message) has hit max_tokens mid-call too often."""
    count = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if truncated_tool_calls(message):
            count += 1
    return count >= MAX_TRUNCATED_RETRIES


def reject_truncated_tool_calls(state: "AgentState") -> dict:
    """Graph node: answer each truncated call with an error so the model retries."""
    return {
        "messages": [
            ToolMessage(
                content=TRUNCATED_TOOL_CALL_MESSAGE,
                tool_call_id=tc["id"],
                name=tc.get("name") or "unknown",
                status="error",
                additional_kwargs={REJECTED_TOOL_CALL_KWARG: True},
            )
            for tc in truncated_tool_calls(state["messages"][-1])
            if tc.get("id")
        ]
    }


class AgentState(TypedDict):
    """State that flows through the Scout agent graph and is checkpointed to the DB.

    All UUID fields are strings because TypedDict values must be JSON-serializable
    for checkpoint persistence.
    """

    # add_messages reducer deduplicates by message ID
    messages: Annotated[list[BaseMessage], add_messages]

    # Injected into every MCP tool call to route to the correct schema
    workspace_id: str

    user_id: str

    # Injected into MCP tool calls that associate background jobs (ThreadJob)
    thread_id: str
