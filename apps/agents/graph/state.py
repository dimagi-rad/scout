"""
Agent state definition for the Scout data agent platform.

Defines the AgentState TypedDict that flows through the LangGraph conversation
graph. All fields are JSON-serializable for Postgres checkpoint persistence.
"""

from typing import Annotated

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from apps.agents.graph.attachments import safe_filename
from apps.chat.attachments import merge_attachments, message_attachments
from apps.chat.constants import SYSTEM_RESUME_MARKER

DEFAULT_MAX_MESSAGES = 20

# A reply like "yes", "continue" or "the first one" says nothing on its own, so the
# user's message before it, and the assistant text each one answers, are kept too.
PINNED_USER_MESSAGES = 2

# The note for pruned attachments names the newest few, so an old thread's note
# stays short instead of growing with every file ever attached.
MAX_NOTED_ATTACHMENTS = 10


def prune_messages(
    messages: list[BaseMessage],
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> list[BaseMessage]:
    """Keep system messages plus the most recent ``max_messages`` conversation
    messages, without breaking tool call/response pairs.

    When the dropped messages carried attachments, a short note leads the kept
    window so the model still knows they exist and can read them back.
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

    # A turn of parallel tool calls passes the window within a few rounds; without
    # its request the model saw an "empty message" and answered with a menu (#919).
    request = _current_request(conversation_messages)[-max(max_messages // 2, 1) :]
    start = len(conversation_messages) - max_messages
    if request and request[0] < start:
        start = len(conversation_messages) - max(max_messages - len(request), 1)
    # Skip results whose AIMessage fell out of the window. If that would leave
    # nothing, the window sits inside one round wider than itself: back up to the
    # round's AIMessage, or every result is lost and the model restarts silently.
    skip = start
    while skip < len(conversation_messages) and isinstance(
        conversation_messages[skip], ToolMessage
    ):
        skip += 1
    if skip < len(conversation_messages):
        start = skip
    else:
        while start > 0 and isinstance(conversation_messages[start], ToolMessage):
            start -= 1
    pinned = [conversation_messages[i] for i in request if i < start]
    pruned_conversation = conversation_messages[start:]
    # Only a history that is nothing but ToolMessages still opens on one.
    while pruned_conversation and isinstance(pruned_conversation[0], ToolMessage):
        pruned_conversation = pruned_conversation[1:]
    overflow = len(pinned) + len(pruned_conversation) - max_messages
    if overflow > 0 and pinned:
        pinned = _shed_pins(pinned, overflow)

    kept = {id(message) for message in (*pinned, *pruned_conversation)}
    note = dropped_attachments_note([m for m in conversation_messages if id(m) not in kept])
    return system_messages + ([note] if note is not None else []) + pinned + pruned_conversation


def _describe_attachment(entry: dict) -> str:
    name = f'"{safe_filename(entry.get("filename"))}"'
    if str(entry.get("media_type", "")).startswith("image/"):
        return f"the image {name} (id {entry['id']})"
    size = entry.get("size")
    size_text = f", {size:,} bytes" if isinstance(size, int) else ""
    return f"{name} (id {entry['id']}{size_text})"


def dropped_attachments_note(dropped: list[BaseMessage]) -> HumanMessage | None:
    """A note naming the attachments of messages pruned from the window, or None."""
    entries = merge_attachments(
        *(message_attachments(m) for m in dropped if isinstance(m, HumanMessage))
    )
    if not entries:
        return None
    shown = entries[-MAX_NOTED_ATTACHMENTS:]
    names = "; ".join(_describe_attachment(e) for e in shown)
    earlier = len(entries) - len(shown)
    more = f" and {earlier} earlier files" if earlier else ""
    return HumanMessage(
        content=(
            f"[Earlier in this chat the user attached {names}{more}. Those messages are no "
            "longer shown. Call read_attachment with an id to read a text file again; "
            "ask the user to attach an image again if you need to look at it.]"
        )
    )


def _shed_pins(pinned: list[BaseMessage], count: int) -> list[BaseMessage]:
    """Drop up to ``count`` resume markers and assistant-text pins, oldest first.

    The user's own messages are never shed: whichever one carries the request,
    losing it is the #919 failure. ``_current_request``'s cap bounds the overflow.
    """
    droppable = [
        k
        for k, message in enumerate(pinned)
        if not isinstance(message, HumanMessage) or _is_resume_marker(message)
    ]
    # With no user message pinned, the latest marker is the user turn the API needs.
    if not any(isinstance(m, HumanMessage) and not _is_resume_marker(m) for m in pinned):
        markers = [k for k in droppable if isinstance(pinned[k], HumanMessage)]
        if markers:
            droppable.remove(markers[-1])
    dropped = set(droppable[:count])
    return [message for k, message in enumerate(pinned) if k not in dropped]


def _is_resume_marker(message: BaseMessage) -> bool:
    content = message.content
    return isinstance(content, str) and content.startswith(SYSTEM_RESUME_MARKER)


def _current_request(conversation: list[BaseMessage]) -> list[int]:
    """Indices of the human messages that state what the current turn must answer.

    That is the latest run of consecutive HumanMessages plus the user's last
    ``PINNED_USER_MESSAGES`` own messages (a resume marker is not one), each with
    the tool-free assistant reply it answers.
    """
    humans = [i for i, m in enumerate(conversation) if isinstance(m, HumanMessage)]
    if not humans:
        return []
    first = humans[-1]
    while first > 0 and isinstance(conversation[first - 1], HumanMessage):
        first -= 1
    asked = [i for i in humans if not _is_resume_marker(conversation[i])][-PINNED_USER_MESSAGES:]
    answered = [
        i - 1
        for i in asked
        if i > 0
        and isinstance(conversation[i - 1], AIMessage)
        and not all_tool_calls(conversation[i - 1])
    ]
    return sorted({*range(first, humans[-1] + 1), *asked, *answered})


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
# otherwise retry at 16k output tokens a pass until the recursion limit. Two
# truncated turns in a row means one retry.
MAX_CONSECUTIVE_TRUNCATED_TURNS = 2


def truncated_retries_exhausted(messages: list[BaseMessage]) -> bool:
    """Whether the latest model turns were cut off mid-call too many times in a row.

    Only consecutive truncations count, so a run that recovered from one and
    carried on isn't penalised for an unrelated one later.
    """
    count = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if not isinstance(message, AIMessage):
            continue
        if not truncated_tool_calls(message):
            break
        count += 1
    return count >= MAX_CONSECUTIVE_TRUNCATED_TURNS


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
