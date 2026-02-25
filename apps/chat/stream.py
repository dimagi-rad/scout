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

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# Maximum wall-clock time for agent execution before we abort.
AGENT_TIMEOUT_SECONDS = 300  # 5 minutes

# Tools that emit MCP progress notifications and get early card-open treatment.
_PROGRESS_TOOLS = frozenset({"run_materialization"})

# Sentinel used to signal end of the LangGraph event stream.
_STREAM_DONE = object()


def _sse(chunk: dict) -> str:
    """Format a chunk as an SSE data event."""
    return f"data: {json.dumps(chunk)}\n\n"


def _tool_content_to_str(output: Any) -> str:
    """Convert tool output to a readable string for frontend display."""
    if isinstance(output, ToolMessage):
        content = output.content
    else:
        content = output
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
    progress_queue: asyncio.Queue | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream LangGraph agent events as UI Message Stream Protocol (SSE) chunks.

    When progress_queue is provided, progress notifications delivered by the
    MCP on_progress callback are interleaved with LangGraph events so that
    run_materialization step updates appear in the tool card in real time.
    """
    text_id = "text-0"
    text_started = False
    reasoning_id = "reasoning-0"
    reasoning_started = False
    tool_calls_processed: set[str] = set()
    # Maps run_id -> toolCallId for tools whose card was opened on tool_start.
    tool_cards_opened: dict[str, str] = {}
    # toolCallId of the currently in-flight progress tool (if any).
    active_progress_tool_call_id: str | None = None

    # Use a noop queue when no progress queue is supplied so the loop is unified.
    _queue: asyncio.Queue = progress_queue if progress_queue is not None else asyncio.Queue()

    # Preamble
    yield _sse({"type": "start"})
    yield _sse({"type": "start-step"})

    event_stream = agent.astream_events(input_state, config=config, version="v2")

    async def _next_event() -> Any:
        try:
            return await event_stream.__anext__()
        except StopAsyncIteration:
            return _STREAM_DONE

    try:
        deadline = asyncio.get_event_loop().time() + AGENT_TIMEOUT_SECONDS

        lg_task: asyncio.Task = asyncio.create_task(_next_event())
        pg_task: asyncio.Task = asyncio.create_task(_queue.get())

        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"Agent execution exceeded {AGENT_TIMEOUT_SECONDS}s timeout")

            done, _ = await asyncio.wait({lg_task, pg_task}, return_when=asyncio.FIRST_COMPLETED)

            # Process progress items first so they appear before the tool result.
            if pg_task in done:
                progress = pg_task.result()
                if progress is not None and active_progress_tool_call_id:
                    msg = progress.get("message", "")
                    current = int(progress.get("current", 0))
                    total = int(progress.get("total", 0))
                    text = f"⏳ {msg} ({current}/{total})" if total else f"⏳ {msg}"
                    yield _sse(
                        {
                            "type": "tool-output-available",
                            "toolCallId": active_progress_tool_call_id,
                            "output": text,
                        }
                    )
                pg_task = asyncio.create_task(_queue.get())

            if lg_task not in done:
                continue

            event = lg_task.result()

            if event is _STREAM_DONE:
                pg_task.cancel()
                break

            lg_task = asyncio.create_task(_next_event())
            event_type = event.get("event")

            # ── on_tool_start: open card early for progress-enabled tools ──────
            if event_type == "on_tool_start":
                tool_name = event.get("name", "")
                run_id = event.get("run_id")
                if tool_name in _PROGRESS_TOOLS and run_id:
                    tool_call_id = uuid.uuid4().hex
                    tool_cards_opened[run_id] = tool_call_id
                    active_progress_tool_call_id = tool_call_id
                    yield _sse(
                        {
                            "type": "tool-input-available",
                            "toolCallId": tool_call_id,
                            "toolName": tool_name,
                            "input": {},
                        }
                    )
                continue

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

                # Drain any remaining progress items before emitting the final result.
                if active_progress_tool_call_id:
                    while not _queue.empty():
                        try:
                            progress = _queue.get_nowait()
                            msg = progress.get("message", "")
                            current = int(progress.get("current", 0))
                            total = int(progress.get("total", 0))
                            text = f"⏳ {msg} ({current}/{total})" if total else f"⏳ {msg}"
                            yield _sse(
                                {
                                    "type": "tool-output-available",
                                    "toolCallId": active_progress_tool_call_id,
                                    "output": text,
                                }
                            )
                        except asyncio.QueueEmpty:
                            break

                # Reuse the toolCallId if this card was pre-opened on tool_start.
                tool_call_id = tool_cards_opened.pop(run_id, None) if run_id else None
                if tool_call_id is None:
                    tool_call_id = run_id or uuid.uuid4().hex
                    yield _sse(
                        {
                            "type": "tool-input-available",
                            "toolCallId": tool_call_id,
                            "toolName": tool_name,
                            "input": {},
                        }
                    )

                # Clear active progress tracker when the tool completes.
                if tool_call_id == active_progress_tool_call_id:
                    active_progress_tool_call_id = None

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
    except Exception:
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
    finally:
        lg_task.cancel()
        pg_task.cancel()

    # Close any open parts
    if reasoning_started:
        yield _sse({"type": "reasoning-end", "id": reasoning_id})
    if text_started:
        yield _sse({"type": "text-end", "id": text_id})

    # Finish markers
    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish", "finishReason": "stop"})
