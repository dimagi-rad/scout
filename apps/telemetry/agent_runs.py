"""Measure one agent run (a chat turn or a recipe run) in memory, then write it once.

``AgentRunTelemetry`` is a LangChain callback handler, so it sees every tool call
and model call of the run, nested subagents included, from the Django side of the
MCP client. Nothing is written until ``aflush``: one bulk insert of the run's own
event plus one event per tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage

from apps.telemetry import recorder
from apps.telemetry.models import EventKind, Outcome, TelemetryEvent

logger = logging.getLogger(__name__)

# Tool output can be large; the envelope's status and error code lead it and its
# server timing ends it, so only those slices are searched.
_HEAD_CHARS = 400
_TAIL_CHARS = 120
_FAILED_ENVELOPE = re.compile(r'^\s*\{\s*"success"\s*:\s*false')
_ERROR_CODE = re.compile(r'"code"\s*:\s*"([A-Za-z0-9_]{1,48})"')
_SERVER_TIMING = re.compile(r'"timing_ms"\s*:\s*(\d{1,9})')

# Chunks the chat stream yields, as ``_sse`` serialises them.
_TEXT_CHUNKS = ('data: {"type": "text-delta"', 'data: {"type": "reasoning-delta"')
_ERROR_CHUNK = 'data: {"type": "error"'
_STATUS_CHUNK = 'data: {"type": "data-chat-status"'
_FINISH_CHUNK = 'data: {"type": "finish"'


def _tool_text(output: Any) -> str:
    content = output.content if isinstance(output, ToolMessage) else output
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
            if isinstance(block, str):
                return block
    return ""


def _tool_result(output: Any) -> tuple[str, dict[str, Any]]:
    """The tool call's outcome and the envelope's error code and server timing, if any."""
    attrs: dict[str, Any] = {}
    failed = isinstance(output, ToolMessage) and output.status == "error"
    text = _tool_text(output)
    head = text[:_HEAD_CHARS]
    if _FAILED_ENVELOPE.match(head):
        failed = True
        if match := _ERROR_CODE.search(head):
            attrs["error_code"] = match.group(1)
    if match := _SERVER_TIMING.search(text[-_TAIL_CHARS:]):
        attrs["server_ms"] = int(match.group(1))
    return (Outcome.ERROR if failed else Outcome.OK), attrs


def _usage(response: Any) -> tuple[dict[str, int], str]:
    """Token counts and model name from an ``on_llm_end`` result."""
    counts: dict[str, int] = {}
    model = ""
    for generations in getattr(response, "generations", None) or []:
        for generation in generations:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None) or {}
            counts["input_tokens"] = counts.get("input_tokens", 0) + int(
                usage.get("input_tokens") or 0
            )
            counts["output_tokens"] = counts.get("output_tokens", 0) + int(
                usage.get("output_tokens") or 0
            )
            details = usage.get("input_token_details") or {}
            for source, key in (
                ("cache_read", "cache_read_tokens"),
                ("cache_creation", "cache_creation_tokens"),
            ):
                counts[key] = counts.get(key, 0) + int(details.get(source) or 0)
            metadata = getattr(message, "response_metadata", None) or {}
            model = model or str(metadata.get("model_name") or metadata.get("model") or "")
    return counts, model


class AgentRunTelemetry(BaseCallbackHandler):
    """Collects a run's timings, tool calls and token usage; see the module docstring."""

    # The handler only touches memory, so it runs inline rather than in a thread.
    run_inline = True
    raise_error = False

    def __init__(
        self,
        kind: str,
        *,
        user_id: Any = None,
        workspace_id: Any = None,
        thread_id: str = "",
        name: str = "",
        started: float | None = None,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.thread_id = str(thread_id or "")
        self.name = name
        self.outcome = ""
        self.attrs: dict[str, Any] = {}
        # A live turn starts when the request arrives, before the agent is built.
        self._started = started if started is not None else time.monotonic()
        self._first_token_ms: float | None = None
        self._tool_starts: dict[Any, tuple[str, float, bool]] = {}
        self._tool_events: list[TelemetryEvent | None] = []
        self._tool_errors = 0
        self._llm_calls = 0
        self._llm_errors = 0
        self._tokens: dict[str, int] = {}
        self._model = ""
        self._flushed = False

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._started) * 1000

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        # Without this, LangChain falls back to on_llm_start and first renders the
        # whole message history to strings, on the event loop, for every model call.
        return None

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs) -> None:
        name = kwargs.get("name") or (serialized or {}).get("name") or "unknown"
        nested = bool((kwargs.get("metadata") or {}).get("subagent"))
        self._tool_starts[run_id] = (str(name), time.monotonic(), nested)

    def on_tool_end(self, output, *, run_id, **kwargs) -> None:
        outcome, attrs = _tool_result(output)
        self._end_tool(run_id, outcome, attrs)

    def on_tool_error(self, error, *, run_id, **kwargs) -> None:
        self._end_tool(run_id, Outcome.ERROR, {"error_type": type(error).__name__})

    def _end_tool(self, run_id, outcome: str, attrs: dict[str, Any]) -> None:
        started = self._tool_starts.pop(run_id, None)
        if started is None:
            return
        if outcome == Outcome.ERROR:
            self._tool_errors += 1
        self._tool_events.append(self._tool_event(started, outcome, attrs))

    def _tool_event(self, started, outcome: str, attrs: dict[str, Any]) -> TelemetryEvent | None:
        name, started_at, nested = started
        return recorder.build_event(
            EventKind.TOOL_CALL,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            name=name,
            outcome=outcome,
            duration_ms=(time.monotonic() - started_at) * 1000,
            attrs={
                "run_kind": self.kind,
                "thread_id": self.thread_id,
                # A subagent's calls also sit inside its parent tool call's time.
                "nested": nested,
                **attrs,
            },
        )

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        self._llm_calls += 1
        counts, model = _usage(response)
        for key, value in counts.items():
            self._tokens[key] = self._tokens.get(key, 0) + value
        self._model = self._model or model

    def on_llm_error(self, error, *, run_id, **kwargs) -> None:
        self._llm_calls += 1
        self._llm_errors += 1

    def observe_chunk(self, chunk: Any) -> None:
        """Note the chat stream's first token, error and finish as the client sees them."""
        if not isinstance(chunk, str):
            return
        if self._first_token_ms is None and chunk.startswith(_TEXT_CHUNKS):
            self._first_token_ms = self._elapsed_ms()
        elif chunk.startswith(_ERROR_CHUNK) or (
            chunk.startswith(_STATUS_CHUNK) and '"retryable-error"' in chunk
        ):
            self.outcome = Outcome.FAILED
        elif chunk.startswith(_FINISH_CHUNK) and not self.outcome:
            self.outcome = Outcome.COMPLETED

    def events(self) -> list[TelemetryEvent | None]:
        # A tool still running when the run was stopped never reports an end
        # (CancelledError skips on_tool_error); these are the slow calls that matter.
        unfinished = [
            self._tool_event(started, Outcome.STOPPED, {}) for started in self._tool_starts.values()
        ]
        tool_events = [*self._tool_events, *unfinished]
        attrs: dict[str, Any] = {
            "thread_id": self.thread_id,
            "tool_calls": len(tool_events),
            "tools_unfinished": len(unfinished),
            "tool_errors": self._tool_errors,
            "llm_calls": self._llm_calls,
            "llm_errors": self._llm_errors,
            **self._tokens,
            **self.attrs,
        }
        if self._first_token_ms is not None:
            attrs["ttft_ms"] = round(self._first_token_ms)
        if self._model:
            attrs["model"] = self._model
        run_event = recorder.build_event(
            self.kind,
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            name=self.name,
            outcome=self.outcome or Outcome.FAILED,
            duration_ms=self._elapsed_ms(),
            attrs=attrs,
        )
        return [run_event, *tool_events]

    async def aflush(self) -> None:
        """Write the run's events once; never raises."""
        if self._flushed:
            return
        self._flushed = True
        try:
            events = self.events()
        except Exception:
            logger.warning("Could not build telemetry for %s", self.kind, exc_info=True)
            return
        await recorder.arecord_events(events)

    async def wrap_stream(
        self, stream: AsyncGenerator[str, None], *, lost: Callable[[], bool] | None = None
    ) -> AsyncIterator[str]:
        """Pass a chat turn's SSE stream through, recording the turn when it ends.

        The write happens after the last chunk is sent and after the turn lease is
        released, so it never delays the reply's content or the next turn; it does
        hold the response open for one insert. ``lost`` tells a stop by the user
        from a turn that lost its thread to another run.
        """
        self.attrs["setup_ms"] = round(self._elapsed_ms())
        try:
            async with contextlib.aclosing(stream) as chunks:
                async for chunk in chunks:
                    self.observe_chunk(chunk)
                    yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            if not self.outcome:
                self.outcome = Outcome.STOPPED
            if lost is not None and lost():
                self.attrs["lease_lost"] = True
            raise
        except Exception:
            self.outcome = Outcome.FAILED
            raise
        finally:
            await self.aflush()

    @contextlib.asynccontextmanager
    async def recording(self):
        """Time a background run (a resumed or flushed turn) and record it on exit.

        A clean exit is completed and an error failed; the error still propagates.
        Nobody can press stop on a background run, so a cancellation (a deadline,
        a lost lease, a worker shutdown) is failed too, flagged ``cancelled``.
        """
        self._started = time.monotonic()
        try:
            yield self
        except asyncio.CancelledError:
            self.outcome = Outcome.FAILED
            self.attrs["cancelled"] = True
            raise
        except Exception:
            self.outcome = Outcome.FAILED
            raise
        else:
            self.outcome = self.outcome or Outcome.COMPLETED
        finally:
            await self.aflush()

    def with_callbacks(self, config: dict) -> Any:
        """``config`` with this handler added to its callbacks."""
        config["callbacks"] = [*config.get("callbacks", []), self]
        return config


def background_turn(name: str, *, user, workspace, thread_id) -> AgentRunTelemetry:
    return AgentRunTelemetry(
        EventKind.CHAT_TURN,
        user_id=user.id,
        workspace_id=workspace.id,
        thread_id=str(thread_id),
        name=name,
    )
