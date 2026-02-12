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
  - {"type":"tool-input-available","toolCallId":"<id>","toolName":"<name>","input":{...}}
  - {"type":"tool-output-available","toolCallId":"<id>","output":{...}}
  - {"type":"finish-step"}
  - {"type":"finish","finishReason":"stop"}
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncGenerator

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


def _sse(chunk: dict) -> str:
    """Format a chunk as an SSE data event."""
    return f"data: {json.dumps(chunk)}\n\n"


def _tool_content_to_str(output: Any) -> str:
    """Convert tool output to a string."""
    if isinstance(output, ToolMessage):
        return output.content if isinstance(output.content, str) else str(output.content)
    if isinstance(output, str):
        return output
    return str(output)


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
    tool_calls_processed: set[str] = set()

    # Preamble
    yield _sse({"type": "start"})
    yield _sse({"type": "start-step"})

    try:
        async for event in agent.astream_events(input_state, config=config, version="v2"):
            event_type = event.get("event")

            if event_type == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if not chunk or not hasattr(chunk, "content") or not chunk.content:
                    continue

                # Extract text from content (string or list of blocks)
                texts: list[str] = []
                if isinstance(chunk.content, str):
                    texts.append(chunk.content)
                elif isinstance(chunk.content, list):
                    for block in chunk.content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text", "")
                            if t:
                                texts.append(t)
                        elif hasattr(block, "text") and block.text:
                            texts.append(block.text)

                for t in texts:
                    if not text_started:
                        yield _sse({"type": "text-start", "id": text_id})
                        text_started = True
                    yield _sse({"type": "text-delta", "id": text_id, "delta": t})

            elif event_type == "on_tool_end":
                run_id = event.get("run_id")
                if run_id and run_id in tool_calls_processed:
                    continue
                if run_id:
                    tool_calls_processed.add(run_id)

                # Close any open text part before tool output
                if text_started:
                    yield _sse({"type": "text-end", "id": text_id})
                    text_started = False
                    text_id = f"text-{uuid.uuid4().hex[:8]}"

                tool_output = event.get("data", {}).get("output")
                if tool_output:
                    content = _tool_content_to_str(tool_output)
                    tool_name = event.get("name", "unknown")
                    tool_call_id = run_id or uuid.uuid4().hex

                    yield _sse({
                        "type": "tool-input-available",
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "input": {},
                    })
                    yield _sse({
                        "type": "tool-output-available",
                        "toolCallId": tool_call_id,
                        "output": content[:2000],
                    })

    except Exception:
        logger.exception("Error during agent streaming")
        if not text_started:
            yield _sse({"type": "text-start", "id": text_id})
            text_started = True
        yield _sse({
            "type": "text-delta",
            "id": text_id,
            "delta": "\n\nAn error occurred while processing your request.",
        })

    # Close any open text part
    if text_started:
        yield _sse({"type": "text-end", "id": text_id})

    # Finish markers
    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish", "finishReason": "stop"})
