"""
Agent state definition for the Scout data agent platform.

Defines the AgentState TypedDict that flows through the LangGraph conversation
graph. All fields are JSON-serializable for Postgres checkpoint persistence.
"""

from typing import Annotated

from langchain_core.messages import BaseMessage, SystemMessage
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
    user_role: str  # viewer | analyst | admin

    # Injected into MCP tool calls that associate background jobs (ThreadJob)
    thread_id: str
