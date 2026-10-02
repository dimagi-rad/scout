"""Streaming a background resume's answer into an open chat.

A resume (or the held-request flush) runs in a worker, not in a chat request,
so the chat cannot read its token stream directly. The worker writes the text it
streams to ResumeStreamChunk rows in small batches, and the chat tails them by id
(GET .../resume-stream/?after=<id>), so a reconnect resumes where it left off and
any number of tabs can follow. The final message still comes from the checkpoint
once the run ends; these rows are best-effort and pruned soon after.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from datetime import timedelta

from django.utils import timezone
from langchain_core.messages import AIMessageChunk

from apps.agents.graph.base import AGENT_NODE
from apps.chat.models import ResumeStreamChunk

logger = logging.getLogger(__name__)

# Small enough to read as streaming, large enough not to write a row per token.
FLUSH_INTERVAL_SECONDS = 0.25
FLUSH_CHARS = 400
# The client reads a full page as "more to come" (``more`` in the response).
READ_LIMIT = 500
RETENTION = timedelta(minutes=30)
# Only the graph's own answer: tools may run models of their own.
STREAMED_NODE = AGENT_NODE
# Between the answer's model calls (text, tools, more text), as the reload shows them.
CALL_SEPARATOR = "\n\n"


def _is_answer(chunk, metadata: dict) -> bool:
    """A token of the top-level agent's answer, not of a subagent tool's own graph.

    Subagent graphs name their node "agent" too and inherit the run's callbacks,
    so the node alone does not tell them apart (see stream.py's equivalent check).
    """
    if not isinstance(chunk, AIMessageChunk) or metadata.get("langgraph_node") != STREAMED_NODE:
        return False
    tags = metadata.get("tags") or []
    return not metadata.get("subagent") and "subagent" not in tags


def chunk_text(chunk) -> str:
    """The answer text in a streamed model chunk (thinking and tool calls aside)."""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


class ResumeStreamWriter:
    """Batches a run's text into rows. Never raises: streaming is a nicety."""

    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.run = uuid.uuid4()
        self._buffer: list[str] = []
        self._buffered = 0
        self._last_flush = time.monotonic()
        self._broken = False

    async def feed(self, text: str) -> None:
        if not text or self._broken:
            return
        self._buffer.append(text)
        self._buffered += len(text)
        if (
            self._buffered >= FLUSH_CHARS
            or time.monotonic() - self._last_flush >= FLUSH_INTERVAL_SECONDS
        ):
            await self._flush()

    async def close(self) -> None:
        await self._flush(done=True)

    async def _flush(self, *, done: bool = False) -> None:
        # The done row is still tried after a failed write: without it a reader
        # would take the run's partial text for an answer still being written.
        if (self._broken and not done) or (not self._buffer and not done):
            return
        # After a gap, more text would read as joined to what came before it.
        text = "" if self._broken else "".join(self._buffer)
        self._buffer, self._buffered = [], 0
        self._last_flush = time.monotonic()
        try:
            await ResumeStreamChunk.objects.acreate(
                thread_id=self.thread_id, run=self.run, text=text, done=done
            )
        except Exception:
            # The answer still lands in the checkpoint; only the live view is lost.
            self._broken = True
            logger.warning(
                "Could not stream the resume of thread %s", self.thread_id, exc_info=True
            )


async def arun_streamed(agent, input_state: dict, config: dict, thread_id) -> dict | None:
    """``agent.ainvoke`` that also streams the answer's text for open chats.

    Returns the final state, as ``ainvoke`` does, and raises what it raises.
    """
    writer = ResumeStreamWriter(thread_id)
    final = None
    call_id = None
    try:
        # aclosing: a timeout or cancel must close the graph run here, not at GC.
        async with contextlib.aclosing(
            agent.astream(input_state, config, stream_mode=["messages", "values"])
        ) as stream:
            async for mode, payload in stream:
                if mode == "values":
                    final = payload
                    continue
                chunk, metadata = payload
                if not _is_answer(chunk, metadata or {}):
                    continue
                text = chunk_text(chunk)
                if text and chunk.id != call_id:
                    if call_id is not None:
                        text = CALL_SEPARATOR + text
                    call_id = chunk.id
                await writer.feed(text)
    finally:
        await writer.close()
    return final


async def aread_after(thread_id, after_id: int) -> list[dict]:
    """Rows after ``after_id``; with none read yet, only the thread's latest run's,
    so a chat catching up never pages through answers it already shows."""
    rows = ResumeStreamChunk.objects.filter(thread_id=thread_id, id__gt=after_id)
    if after_id == 0:
        latest = await rows.order_by("-id").values_list("run", flat=True).afirst()
        if latest is None:
            return []
        rows = rows.filter(run=latest)
    return [
        {"id": row.id, "run": str(row.run), "text": row.text, "done": row.done}
        async for row in rows.order_by("id")[:READ_LIMIT]
    ]


async def aprune() -> int:
    deleted, _ = await ResumeStreamChunk.objects.filter(
        created_at__lt=timezone.now() - RETENTION
    ).adelete()
    return deleted
