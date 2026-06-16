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
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from anthropic import APIStatusError, InternalServerError, RateLimitError
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# Maximum wall-clock time for agent execution before we abort.
AGENT_TIMEOUT_SECONDS = 300  # 5 minutes

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
    """Format a chunk as an SSE data event."""
    return f"data: {json.dumps(chunk)}\n\n"


def _tool_content_to_str(output: Any) -> str:
    """Convert tool output to a readable string for frontend display."""
    content = output.content if isinstance(output, ToolMessage) else output
    if isinstance(content, str):
        return _try_pretty_json(content)
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(_try_pretty_json(block.get("text", "")))
            elif isinstance(block, str):
                texts.append(_try_pretty_json(block))
            else:
                texts.append(json.dumps(block, indent=2, default=str))
        return "\n".join(texts)
    return json.dumps(content, indent=2, default=str)


def _try_pretty_json(s: str) -> str:
    """If s is a JSON string, return it pretty-printed. Otherwise return as-is."""
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict | list):
            return json.dumps(parsed, indent=2, default=str)
    except (json.JSONDecodeError, TypeError):
        pass
    return s


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

    # Preamble
    yield _sse({"type": "start"})
    yield _sse({"type": "start-step"})

    event_stream = agent.astream_events(input_state, config=config, version="v2")

    try:
        deadline = asyncio.get_event_loop().time() + AGENT_TIMEOUT_SECONDS

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"Agent execution exceeded {AGENT_TIMEOUT_SECONDS}s timeout")
            try:
                event = await event_stream.__anext__()
            except StopAsyncIteration:
                break

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

                audit_logger.info(
                    "tool_call tool=%s user_id=%s thread_id=%s project_id=%s",
                    tool_name,
                    input_state.get("user_id", ""),
                    config.get("configurable", {}).get("thread_id", ""),
                    input_state.get("project_id", ""),
                )

                tool_call_id = run_id or uuid.uuid4().hex
                yield _sse(
                    {
                        "type": "tool-input-available",
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "input": {},
                    }
                )

                truncated = len(content) > 2000
                display_content = content[:2000]
                if truncated:
                    display_content += f"\n\n... (truncated, {len(content)} chars total)"
                yield _sse(
                    {
                        "type": "tool-output-available",
                        "toolCallId": tool_call_id,
                        "output": display_content,
                    }
                )

    except TimeoutError:
        logger.warning("Agent execution timed out after %ds", AGENT_TIMEOUT_SECONDS)
        if reasoning_started:
            yield _sse({"type": "reasoning-end", "id": reasoning_id})
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
            logger.exception("Error during agent streaming")
            if reasoning_started:
                yield _sse({"type": "reasoning-end", "id": reasoning_id})
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

    # Close any open parts
    if reasoning_started:
        yield _sse({"type": "reasoning-end", "id": reasoning_id})
    if text_started:
        yield _sse({"type": "text-end", "id": text_id})

    # Finish markers
    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish", "finishReason": "stop"})
