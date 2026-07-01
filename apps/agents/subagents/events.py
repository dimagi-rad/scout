"""Runtime event queue for nested subagent streams.

The chat SSE bridge owns the queue for a parent graph run. Local tools can emit
subagent events into that queue while they run, and ``apps.chat.stream`` merges
them with parent LangGraph events.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

SUBAGENT_EVENT_QUEUE_CONFIG_KEY = "subagent_event_queue"

_current_event_queue: ContextVar[asyncio.Queue | None] = ContextVar(
    "scout_subagent_event_queue",
    default=None,
)


def set_subagent_event_queue(queue: asyncio.Queue | None):
    """Bind a subagent event queue for the current graph tool execution."""
    return _current_event_queue.set(queue)


def reset_subagent_event_queue(token) -> None:
    _current_event_queue.reset(token)


def get_subagent_event_queue() -> asyncio.Queue | None:
    return _current_event_queue.get()


async def emit_subagent_event(event: dict[str, Any]) -> None:
    """Emit one nested subagent envelope if a chat stream is listening."""
    queue = get_subagent_event_queue()
    if queue is None:
        return
    await queue.put({"source": "subagent", "event": event})

