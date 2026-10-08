"""Streaming a background resume's answer, or a chat turn's, into an open chat.

A resume (or the held-request flush) runs in a worker, not in a chat request,
so the chat cannot read its token stream directly. The worker writes the text it
streams to ResumeStreamChunk rows in small batches, and the chat tails them by id
(GET .../resume-stream/?after=<id>), so a reconnect resumes where it left off and
any number of tabs can follow. The final message still comes from the checkpoint
once the run ends; these rows are best-effort and pruned soon after.

A chat turn's text is written the same way by ``LiveTurnWriter``, for chats that
are not the one running it: another tab or device, or one reopened after a reload
or a route change (#856).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
# A chat turn writes at most one row per interval while it streams text, and none
# while tools run: the platform database is shared and short of connections.
LIVE_FLUSH_INTERVAL_SECONDS = 1.0
# Writing the last row and clearing the run must never hold up the end of a turn.
LIVE_CLOSE_TIMEOUT_SECONDS = 2.0
# The end of a text part ends its model call's text: the stream ends one only for a
# tool (some tools send their part only once they finish, or none if they fail),
# thinking (which comes before a call's text, never after), a retried or fixed reply,
# or the end of the turn. A tool part covers the rest.
_CALL_ENDS = frozenset({"text-end", "tool-input-available", "tool-output-available"})


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


class LiveTurnWriter:
    """Writes a chat turn's answer text for chats that did not start it. Never raises.

    Each model call's text is a run of its own: by the time the next call streams,
    the checkpoint holds the last one's message, so a chat that loads the thread
    mid-turn shows it from there and tails only the call still being written
    (``aread_after`` starts at the latest run). A chat already tailing reloads when
    a new run starts, which brings in the finished call and its tool cards.

    ``observe`` takes the turn's stream parts as the chat sends them and only
    buffers; a background task writes what is buffered at most once per
    ``LIVE_FLUSH_INTERVAL_SECONDS``, so the turn's stream never waits on the
    database. The writes run on the request's own database connection (async ORM
    calls share the request's thread), so writing opens no connection. ``close``
    writes the done row, on which a chat tailing the turn reloads, and deletes the
    thread's earlier turns' text rows. This turn's stay until the next turn or the
    prune: a chat that reads them late would otherwise show only the last row's
    text as the whole answer until its reload lands.
    """

    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.run = uuid.uuid4()
        self.rows_written = 0
        # (run, text, ends the run) in order; a flush can span two calls.
        self._buffer: list[tuple[uuid.UUID, str, bool]] = []
        # This turn's first row: the close clears earlier turns' rows, and must not
        # touch a run that took the thread after this one let go of it.
        self._first_row_id: int | None = None
        self._has_text = False
        self._run_has_text = False
        self._run_ended = False
        self._broken = False
        self._stop = asyncio.Event()
        self._flusher: asyncio.Task | None = None

    def observe(self, sse_chunk: str) -> None:
        # Only these parts matter; skip parsing the rest (a tool's output can be large).
        head = sse_chunk[:40]
        if self._broken or not ('"text-' in head or '"tool-' in head):
            return
        try:
            part = json.loads(sse_chunk.removeprefix("data: "))
        except ValueError:
            return
        if not isinstance(part, dict):
            return
        kind = part.get("type")
        if kind in _CALL_ENDS:
            # Ended now, not when the next call's text starts: a chat that loads the
            # thread while the tool runs has this call from history already.
            self._end_run()
            return
        if kind != "text-delta":
            return
        text = part.get("delta")
        if not isinstance(text, str) or not text:
            return
        if self._run_ended:
            self.run = uuid.uuid4()
            self._run_ended = False
        self._run_has_text = True
        self._has_text = True
        self._buffer.append((self.run, text, False))
        if self._flusher is None:
            self._flusher = asyncio.ensure_future(self._flush_periodically())

    def _end_run(self) -> None:
        if self._run_has_text and not self._run_ended:
            self._buffer.append((self.run, "", True))
            self._run_ended = True
            self._run_has_text = False

    async def close(self) -> None:
        if not self._has_text:
            return
        closing = asyncio.ensure_future(self._finish())
        try:
            # Shielded: the stream closes from a cancelled task when the client leaves,
            # and an unwritten done row would read as an answer still being written.
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            # Still within the request (bounded by the close timeout), so its
            # writes stay on the request's connection.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await closing
            raise
        except Exception:
            logger.warning("Could not close the live stream", exc_info=True)

    async def _flush_periodically(self) -> None:
        while not self._broken and not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), LIVE_FLUSH_INTERVAL_SECONDS)
            if self._buffer and not self._stop.is_set():
                await self._write(done=False)

    async def _finish(self) -> None:
        try:
            async with asyncio.timeout(LIVE_CLOSE_TIMEOUT_SECONDS):
                # Stopped, not cancelled: a write in progress finishes rather than
                # dropping its text between two rows.
                self._stop.set()
                if self._flusher is not None:
                    await self._flusher
                await self._write(done=True)
                if self._first_row_id is not None:
                    await ResumeStreamChunk.objects.filter(
                        thread_id=self.thread_id, done=False, id__lt=self._first_row_id
                    ).adelete()
        except Exception:
            logger.warning(
                "Could not finish the live stream of thread %s", self.thread_id, exc_info=True
            )
        finally:
            if self._flusher is not None and not self._flusher.done():
                self._flusher.cancel()

    async def _write(self, *, done: bool) -> None:
        buffered, self._buffer = self._buffer, []
        # The done row is still tried after a failed write: without it a reader
        # would take the partial text for an answer still being written.
        if self._broken:
            buffered = []
        rows: list[list] = []  # [run, texts, ends]
        for run, text, ends in buffered:
            if rows and rows[-1][0] == run and not rows[-1][2]:
                rows[-1][1].append(text)
                rows[-1][2] = ends
            else:
                rows.append([run, [text], ends])
        # The turn's end: written even for a run a tool already ended, as a chat that
        # reloaded on that end is tailing for this one.
        if done:
            if rows and rows[-1][0] == self.run:
                rows[-1][2] = True
            else:
                rows.append([self.run, [], True])
        try:
            for run, texts, ends in rows:
                row = await ResumeStreamChunk.objects.acreate(
                    thread_id=self.thread_id, run=run, text="".join(texts), done=ends
                )
                if self._first_row_id is None:
                    self._first_row_id = row.id
                self.rows_written += 1
        except Exception:
            # The answer still lands in the checkpoint; only the live view is lost.
            self._broken = True
            logger.warning("Could not stream the turn of thread %s", self.thread_id, exc_info=True)
