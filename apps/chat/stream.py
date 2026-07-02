"""
LangGraph event-to-UI-Message-Stream translator.

Translates LangGraph astream_events(version="v2") into the Vercel AI SDK v6
UI Message Stream Protocol (SSE with JSON chunks) so that the frontend
DefaultChatTransport / useChat hook can parse them.

Each SSE event is:  data: {json}\n\n

Chunk types used:
  - {"type":"start"}
  - {"type":"start-step"}
  - {"type":"text-start","id":"<id>"}
  - {"type":"text-delta","id":"<id>","delta":"<text>"}
  - {"type":"text-end","id":"<id>"}
  - {"type":"reasoning-start","id":"<id>"}
  - {"type":"reasoning-delta","id":"<id>","delta":"<text>"}
  - {"type":"reasoning-end","id":"<id>"}
  - {"type":"tool-input-available","toolCallId":"<id>","toolName":"<name>","input":{...}}
  - {"type":"tool-output-available","toolCallId":"<id>","output":{...}}
  - {"type":"finish-step"}
  - {"type":"finish","finishReason":"stop"}
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from anthropic import APIStatusError, InternalServerError, RateLimitError
from langchain_core.messages import AIMessage, ToolMessage

from apps.agents.graph.base import INJECTED_TOOL_PARAMS
from apps.agents.subagents.events import (
    SUBAGENT_EVENT_QUEUE_CONFIG_KEY,
    SUBAGENT_TOOL_NAMES,
)

logger = logging.getLogger(__name__)

# Maximum wall-clock time for agent execution before we abort.
AGENT_TIMEOUT_SECONDS = 300  # 5 minutes

# Hard cap on the tool-output payload we put on the wire, to bound a runaway
# tool from flooding the SSE stream / browser. Generous enough that real query
# results (already row-limited upstream by the MCP server) round-trip intact so
# the rich card can parse them LIVE. When we do truncate we emit the RAW prefix
# (not pretty-printed) plus an explicit marker; the frontend then falls back to
# the <pre> view rather than silently rendering a broken card.
TOOL_OUTPUT_MAX_CHARS = 100_000
TOOL_OUTPUT_TRUNCATION_MARKER = "\n\n... (truncated, {total} chars total)"

# Anthropic error.type values that indicate a transient, retryable capacity
# problem (overload / rate limit) rather than a bug on our side.
_TRANSIENT_ERROR_TYPES = {"overloaded_error", "rate_limit_error"}
_TRANSIENT_STATUS_CODES = {429, 503, 529}


def _is_transient_overload(exc: BaseException) -> bool:
    """True for transient Anthropic capacity errors worth retrying.

    On a mid-stream ``error`` SSE event the streaming response opened with HTTP
    200, so anthropic raises a base ``APIStatusError`` whose ``status_code`` is
    200 -- the body's ``error.type`` is then the only reliable signal. The
    subclass / status-code checks cover the non-streaming (real HTTP status)
    path.
    """
    if isinstance(exc, RateLimitError | InternalServerError):
        return True
    if isinstance(exc, APIStatusError):
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("type") in _TRANSIENT_ERROR_TYPES:
                return True
        if getattr(exc, "status_code", None) in _TRANSIENT_STATUS_CODES:
            return True
    return False


def _sse(chunk: dict) -> str:
    """Format a chunk as an SSE data event.

    ``default=str`` is a backstop: no single non-serializable value (e.g. a
    langgraph ToolRuntime that slipped into a tool input) may ever crash the
    whole stream -- it is coerced to its string form rather than raising.
    """
    return f"data: {json.dumps(chunk, default=str)}\n\n"


def _error_ref(exc: BaseException) -> str:
    """Mint a short correlation ref for a stream failure.

    Matches the hashing used by the non-streaming error paths in
    ``apps/chat/views.py`` so an operator can cross-reference the user-visible
    ref against the logged exception.
    """
    return hashlib.sha256(f"{time.time()}{exc}".encode()).hexdigest()[:8]


def _tool_content_to_str(output: Any) -> str:
    """Convert tool output to a compact, parse-safe string for the frontend.

    The frontend rich cards JSON.parse this string, so we MUST NOT mangle it:
    JSON content is left as the tool emitted it (no re-pretty-printing — that
    bloated payloads and pushed valid JSON past the live truncation limit,
    breaking the cards live while they worked on reload). Non-JSON content is
    passed through untouched.
    """
    content = output.content if isinstance(output, ToolMessage) else output
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
            else:
                texts.append(json.dumps(block, default=str))
        return "\n".join(texts)
    return json.dumps(content, default=str)


# Params we never surface in the tool-call card. INJECTED_TOOL_PARAMS are the
# context ids the graph injects; ``runtime`` is the ToolRuntime object that
# langchain_mcp_adapters/langgraph's ToolNode inject into every MCP tool call
# (internal plumbing, and not JSON serializable -- it crashed the whole stream,
# Sentry SCOUT-DJANGO-1V).
_HIDDEN_TOOL_PARAMS = INJECTED_TOOL_PARAMS | {"runtime"}


def _is_json_serializable(value: Any) -> bool:
    """True if ``value`` can be put on the JSON-encoded SSE wire as-is."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _redact_tool_input(raw_input: Any) -> dict:
    """Strip server-injected context params from a tool's input for display.

    The graph injects workspace_id / user_id / thread_id / tool_call_id into
    every MCP tool call, and langgraph's ToolNode injects a ``runtime`` object;
    those are internal plumbing, not arguments the user typed, so they must not
    surface in the tool-call card. Any remaining non-JSON-serializable value is
    dropped too, so one stray object can neither leak nor break the card.
    Returns {} for non-dict input (e.g. positional-only), which the card
    renders as "no input".
    """
    if not isinstance(raw_input, dict):
        return {}
    return {
        k: v
        for k, v in raw_input.items()
        if k not in _HIDDEN_TOOL_PARAMS and _is_json_serializable(v)
    }


def _truncate_tool_output(content: str) -> str:
    """Cap tool-output length without corrupting parse-safe JSON silently.

    Below the cap: returned unchanged so the rich card parses it. Above the
    cap: returns the raw prefix plus an explicit marker. The marker makes the
    payload non-JSON, so the frontend deterministically falls back to the
    <pre> view instead of rendering a half-parsed card.
    """
    if len(content) <= TOOL_OUTPUT_MAX_CHARS:
        return content
    return content[:TOOL_OUTPUT_MAX_CHARS] + TOOL_OUTPUT_TRUNCATION_MARKER.format(
        total=len(content)
    )


def _is_nested_subagent_graph_event(event: dict[str, Any]) -> bool:
    """True for LangGraph events produced inside a parent-facing subagent tool.

    Subagent tools forward their own UI events through ``subagent_event_queue``
    with explicit ``parentToolCallId`` metadata. LangGraph may also surface the
    nested graph's raw events through the parent callback stream. If we translate
    those raw events here, the browser shows child tools as top-level cards and
    the nested version is no longer the single source of truth.
    """
    metadata = event.get("metadata") or {}
    if isinstance(metadata, dict) and metadata.get("subagent"):
        return True
    tags = event.get("tags") or []
    return isinstance(tags, list) and "subagent" in tags


def _with_parent_tool_call_id(
    event: dict[str, Any],
    *,
    parent_tool_call_id: str,
) -> dict[str, Any]:
    """Return a subagent UI chunk linked to the authoritative parent tool id."""
    patched = dict(event)
    data = patched.get("data")
    if isinstance(data, dict):
        patched["data"] = {**data, "parentToolCallId": parent_tool_call_id}
    return patched


def _subagent_parent_tool_call_id(event: dict[str, Any]) -> str | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    parent_id = data.get("parentToolCallId")
    return parent_id if isinstance(parent_id, str) and parent_id else None


audit_logger = logging.getLogger("scout.agent.audit")


async def langgraph_to_ui_stream(
    agent: Any,
    input_state: dict,
    config: dict,
) -> AsyncGenerator[str, None]:
    """
    Stream LangGraph agent events as UI Message Stream Protocol (SSE) chunks.
    """
    text_id = "text-0"
    text_started = False
    reasoning_id = "reasoning-0"
    reasoning_started = False
    tool_calls_processed: set[str] = set()
    # run_id (LangGraph) -> tool_call_id (LLM toolu_ id) emitted on_tool_start.
    # The frontend keys per-card progress / Stop / failure off the toolu_ id
    # (ThreadJob.tool_call_id), so the stream MUST emit that id, not run_id.
    run_to_tool_call_id: dict[str, str] = {}
    # Some local tools do not receive the injected ``tool_call_id`` in their
    # on_tool_start input. Defer their input card until on_tool_end, where the
    # ToolMessage carries the authoritative LLM toolu_ id.
    pending_tool_starts: dict[str, dict[str, Any]] = {}
    # Subagent tools may emit child UI events before the parent local-tool
    # ``toolu_`` id is available to the stream bridge. Buffer those child
    # chunks and stamp them with the authoritative parent id immediately before
    # the parent tool output is emitted.
    pending_subagent_events: list[dict[str, Any]] = []

    # Preamble
    yield _sse({"type": "start"})
    yield _sse({"type": "start-step"})

    event_queue: asyncio.Queue = asyncio.Queue()
    stream_config = {
        **config,
        "configurable": {
            **(config.get("configurable") or {}),
            SUBAGENT_EVENT_QUEUE_CONFIG_KEY: event_queue,
        },
    }
    event_stream = agent.astream_events(input_state, config=stream_config, version="v2")

    async def _pump_parent_events() -> None:
        try:
            async for parent_event in event_stream:
                await event_queue.put({"source": "parent", "event": parent_event})
        except Exception as exc:
            await event_queue.put({"source": "parent_error", "error": exc})
        finally:
            await event_queue.put({"source": "parent_done"})

    parent_pump = asyncio.create_task(_pump_parent_events())

    try:
        deadline = asyncio.get_event_loop().time() + AGENT_TIMEOUT_SECONDS

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"Agent execution exceeded {AGENT_TIMEOUT_SECONDS}s timeout")
            try:
                item = await asyncio.wait_for(
                    event_queue.get(),
                    timeout=max(0.1, deadline - asyncio.get_event_loop().time()),
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"Agent execution exceeded {AGENT_TIMEOUT_SECONDS}s timeout")

            source = item.get("source")
            if source == "subagent":
                event = item.get("event")
                if isinstance(event, dict):
                    parent_id = _subagent_parent_tool_call_id(event)
                    if parent_id and not parent_id.startswith("missing-parent-"):
                        yield _sse(event)
                    else:
                        pending_subagent_events.append(event)
                continue
            if source == "parent_error":
                raise item["error"]
            if source == "parent_done":
                break
            if source != "parent":
                continue

            event = item.get("event") or {}
            if _is_nested_subagent_graph_event(event):
                continue

            event_type = event.get("event")

            # ── on_chat_model_stream ───────────────────────────────────────────
            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if not chunk or not hasattr(chunk, "content") or not chunk.content:
                    continue

                texts: list[str] = []
                thinking_texts: list[str] = []

                if isinstance(chunk.content, str):
                    texts.append(chunk.content)
                elif isinstance(chunk.content, list):
                    for block in chunk.content:
                        if isinstance(block, dict):
                            block_type = block.get("type", "")
                            if block_type == "text":
                                t = block.get("text", "")
                                if t:
                                    texts.append(t)
                            elif block_type == "thinking":
                                t = block.get("thinking", "")
                                if t:
                                    thinking_texts.append(t)
                        elif hasattr(block, "text") and block.text:
                            texts.append(block.text)

                for t in thinking_texts:
                    if text_started:
                        yield _sse({"type": "text-end", "id": text_id})
                        text_started = False
                        text_id = f"text-{uuid.uuid4().hex[:8]}"
                    if not reasoning_started:
                        yield _sse({"type": "reasoning-start", "id": reasoning_id})
                        reasoning_started = True
                    yield _sse({"type": "reasoning-delta", "id": reasoning_id, "delta": t})

                for t in texts:
                    if reasoning_started:
                        yield _sse({"type": "reasoning-end", "id": reasoning_id})
                        reasoning_started = False
                        reasoning_id = f"reasoning-{uuid.uuid4().hex[:8]}"
                    if not text_started:
                        yield _sse({"type": "text-start", "id": text_id})
                        text_started = True
                    yield _sse({"type": "text-delta", "id": text_id, "delta": t})

            # ── on_tool_start ──────────────────────────────────────────────────
            elif event_type == "on_tool_start":
                # Emit the loading-state input part LIVE so loading affordances
                # (and run_materialization's Stop/progress) can render before
                # the tool finishes. The LLM toolu_ id is carried in the input
                # (the graph injects `tool_call_id` into every MCP tool call);
                # falling back to run_id only for non-MCP tools that lack it.
                run_id = event.get("run_id")
                raw_input = event.get("data", {}).get("input")
                tool_call_id = None
                if isinstance(raw_input, dict):
                    tool_call_id = raw_input.get("tool_call_id")

                if text_started:
                    yield _sse({"type": "text-end", "id": text_id})
                    text_started = False
                    text_id = f"text-{uuid.uuid4().hex[:8]}"
                if reasoning_started:
                    yield _sse({"type": "reasoning-end", "id": reasoning_id})
                    reasoning_started = False
                    reasoning_id = f"reasoning-{uuid.uuid4().hex[:8]}"

                if not tool_call_id:
                    if run_id:
                        pending_tool_starts[run_id] = {
                            "toolName": event.get("name", "unknown"),
                            "input": _redact_tool_input(raw_input),
                        }
                        continue
                    tool_call_id = uuid.uuid4().hex

                if run_id:
                    run_to_tool_call_id[run_id] = tool_call_id

                yield _sse(
                    {
                        "type": "tool-input-available",
                        "toolCallId": tool_call_id,
                        "toolName": event.get("name", "unknown"),
                        "input": _redact_tool_input(raw_input),
                    }
                )

            # ── on_tool_end ────────────────────────────────────────────────────
            elif event_type == "on_tool_end":
                run_id = event.get("run_id")
                if run_id and run_id in tool_calls_processed:
                    continue
                if run_id:
                    tool_calls_processed.add(run_id)

                if text_started:
                    yield _sse({"type": "text-end", "id": text_id})
                    text_started = False
                    text_id = f"text-{uuid.uuid4().hex[:8]}"
                if reasoning_started:
                    yield _sse({"type": "reasoning-end", "id": reasoning_id})
                    reasoning_started = False
                    reasoning_id = f"reasoning-{uuid.uuid4().hex[:8]}"

                tool_output = event.get("data", {}).get("output")
                if not tool_output:
                    continue

                content = _tool_content_to_str(tool_output)
                tool_name = event.get("name", "unknown")

                # The agent state carries ``workspace_id`` (the projects->workspaces
                # rename); the old ``project_id`` read was ALWAYS empty, so the
                # workspace attribution on every audited tool call was blank
                # (arch #257, finding 08#8).
                audit_logger.info(
                    "tool_call tool=%s user_id=%s thread_id=%s workspace_id=%s",
                    tool_name,
                    input_state.get("user_id", ""),
                    stream_config.get("configurable", {}).get("thread_id", ""),
                    input_state.get("workspace_id", ""),
                )

                # The ToolMessage carries the authoritative LLM toolu_ id; use
                # it so this output pairs with the input part the frontend keyed
                # its per-card affordances off of. Fall back to the start map /
                # run_id when (rarely) absent.
                output_tool_call_id = getattr(tool_output, "tool_call_id", None)
                started_tool_call_id = run_to_tool_call_id.get(run_id or "")
                tool_call_id = (
                    output_tool_call_id
                    or started_tool_call_id
                    or run_id
                    or uuid.uuid4().hex
                )
                pending_start = pending_tool_starts.pop(run_id or "", None)

                # If on_tool_start never emitted an input part for this call,
                # emit a minimal one now so AI SDK has a part to attach output to.
                if (
                    not started_tool_call_id
                    or (output_tool_call_id and output_tool_call_id != started_tool_call_id)
                ):
                    yield _sse(
                        {
                            "type": "tool-input-available",
                            "toolCallId": tool_call_id,
                            "toolName": tool_name,
                            "input": (pending_start or {}).get("input", {}),
                        }
                    )

                if tool_name in SUBAGENT_TOOL_NAMES and pending_subagent_events:
                    for subagent_event in pending_subagent_events:
                        yield _sse(
                            _with_parent_tool_call_id(
                                subagent_event,
                                parent_tool_call_id=str(tool_call_id),
                            )
                        )
                    pending_subagent_events.clear()

                yield _sse(
                    {
                        "type": "tool-output-available",
                        "toolCallId": tool_call_id,
                        "output": _truncate_tool_output(content),
                    }
                )

            # ── escalation node output ─────────────────────────────────────────
            elif event_type == "on_chain_end" and event.get("name") == "escalate":
                # The terminal ``escalate`` node returns a hardcoded AIMessage
                # rather than calling the LLM, so it emits no on_chat_model_stream
                # event and its message was never turned into a live text-delta —
                # the panic-loop recovery affordance only appeared after reload
                # (06#1). Translate the node's output into a streamed text part so
                # the user sees it during the turn.
                output = event.get("data", {}).get("output") or {}
                esc_messages = output.get("messages", []) if isinstance(output, dict) else []
                esc_text = "".join(
                    m.content
                    for m in esc_messages
                    if isinstance(m, AIMessage) and isinstance(m.content, str)
                )
                if esc_text:
                    if reasoning_started:
                        yield _sse({"type": "reasoning-end", "id": reasoning_id})
                        reasoning_started = False
                        reasoning_id = f"reasoning-{uuid.uuid4().hex[:8]}"
                    if not text_started:
                        yield _sse({"type": "text-start", "id": text_id})
                        text_started = True
                    yield _sse({"type": "text-delta", "id": text_id, "delta": esc_text})

    except TimeoutError as exc:
        ref = _error_ref(exc)
        logger.warning(
            "Agent execution timed out after %ds [ref=%s]", AGENT_TIMEOUT_SECONDS, ref
        )
        if reasoning_started:
            yield _sse({"type": "reasoning-end", "id": reasoning_id})
            reasoning_started = False
        if not text_started:
            yield _sse({"type": "text-start", "id": text_id})
            text_started = True
        yield _sse(
            {
                "type": "text-delta",
                "id": text_id,
                "delta": "\n\nThe request timed out. Try simplifying your question or breaking it into smaller steps.",
            }
        )
        if text_started:
            yield _sse({"type": "text-end", "id": text_id})
            text_started = False
        # Emit a native AI SDK error chunk so useChat's error state fires — a
        # timed-out run MUST be distinguishable from a successful one (06#4).
        # Without this the apology above was just message text followed by a
        # normal finishReason 'stop', indistinguishable from success.
        yield _sse(
            {
                "type": "error",
                "errorText": f"The request timed out. Ref: {ref}",
            }
        )
    except Exception as exc:
        if _is_transient_overload(exc):
            # Anthropic was momentarily overloaded / rate-limited -- a transient
            # upstream condition, not a bug. Log at WARNING (so it does not page
            # via Sentry's ERROR-level capture) and emit a transient data part
            # the frontend can use to auto-retry the turn. Any open text/
            # reasoning part is closed by the shared block below.
            logger.warning(
                "Anthropic capacity error during stream (retryable): %s",
                exc.__class__.__name__,
            )
            yield _sse(
                {
                    "type": "data-chat-status",
                    "data": {"kind": "retryable-error", "reason": "overloaded"},
                    "transient": True,
                }
            )
        else:
            ref = _error_ref(exc)
            logger.exception("Error during agent streaming [ref=%s]", ref)
            if reasoning_started:
                yield _sse({"type": "reasoning-end", "id": reasoning_id})
                reasoning_started = False
            if not text_started:
                yield _sse({"type": "text-start", "id": text_id})
                text_started = True
            yield _sse(
                {
                    "type": "text-delta",
                    "id": text_id,
                    "delta": "\n\nAn error occurred while processing your request.",
                }
            )
            if text_started:
                yield _sse({"type": "text-end", "id": text_id})
                text_started = False
            # Emit a native AI SDK error chunk so useChat's error state fires —
            # a failed agent run MUST be distinguishable from a successful one
            # (06#4). The apology text above is informational; this is what makes
            # the frontend show an error state instead of a clean finish.
            yield _sse(
                {
                    "type": "error",
                    "errorText": f"An error occurred while processing your request. Ref: {ref}",
                }
            )
    finally:
        if not parent_pump.done():
            parent_pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await parent_pump

    # Close any open parts
    if reasoning_started:
        yield _sse({"type": "reasoning-end", "id": reasoning_id})
    if text_started:
        yield _sse({"type": "text-end", "id": text_id})

    # Finish markers
    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish", "finishReason": "stop"})
