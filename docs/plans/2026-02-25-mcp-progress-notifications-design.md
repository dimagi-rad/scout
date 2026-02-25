# MCP Progress Notifications — Design

Surface `run_materialization` progress into the frontend tool card via the
`langchain-mcp-adapters` `Callbacks` API.

## Problem

`run_materialization` can take several minutes. The frontend currently shows a
loading indicator until the tool call completes, then displays the final result
in one shot. Users have no visibility into which phase is running.

MCP progress notifications (`ctx.report_progress`) are already emitted by the
server at each pipeline step. `langchain-mcp-adapters` supports forwarding them
via `Callbacks(on_progress=...)` at client construction time. The client is
currently built without a callback, so all progress events are silently dropped.

## Approach

Connect the existing MCP progress events through to the frontend tool card
using an `asyncio.Queue` as a bridge between the MCP callback and the SSE
stream. No frontend changes are required — the Vercel AI SDK replaces
`part.output` on each `tool-output-available` event for the same `toolCallId`.

## Data Flow

```
MCP server: ctx.report_progress(current, total, message)
    ↓  streamable-http transport
langchain-mcp-adapters: async on_progress(progress, total, message, context)
    ↓  progress_queue.put_nowait(...)
langgraph_to_ui_stream: asyncio.wait(FIRST_COMPLETED) loop
    ↓  tool-output-available SSE chunk (same toolCallId)
Frontend: tool card output text replaced in place
```

## Component Changes

### `apps/agents/mcp_client.py`

Drop the `MultiServerMCPClient` singleton. `get_mcp_tools()` accepts an
optional `on_progress` async callable and creates a fresh client per call,
passing `callbacks=Callbacks(on_progress=on_progress)` when provided.

The circuit breaker (module-level failure counters) is retained — it protects
against a repeatedly unavailable MCP server without needing a cached client.

### `apps/chat/views.py`

Before calling `get_mcp_tools()`, create a per-request `asyncio.Queue` and an
`on_progress` coroutine that puts `{current, total, message}` dicts into it.
Pass `on_progress` to `get_mcp_tools()` and `progress_queue` to
`langgraph_to_ui_stream()`.

### `apps/chat/stream.py`

Two additions:

**Early card open.** Handle `on_tool_start` for `run_materialization`: emit
`tool-input-available` immediately so the card opens in loading state. Track
opened cards in a `tool_cards_opened` set; `on_tool_end` skips
`tool-input-available` for cards already opened this way.

**Concurrent loop.** When `progress_queue` is provided, replace the `async for`
over `astream_events` with an `asyncio.wait(FIRST_COMPLETED)` loop over two
tasks:

- `Task A`: `astream.__anext__()` — existing LangGraph event handling, unchanged
- `Task B`: `progress_queue.get()` — emits `tool-output-available` with progress
  text (e.g. `"⏳ Loading cases... (2/5)"`) for the active `run_materialization`
  `toolCallId`

A `None` sentinel put into the queue by the view signals end-of-stream so
`Task B` can be cancelled cleanly.

Only `run_materialization` gets the early-open / in-flight progress path. All
other tools continue to use the existing `on_tool_end`-only handling.

## Scope

- No frontend changes
- No new SSE chunk types
- No database or model changes
- No changes to the MCP server itself

## Files Changed

| File | Change |
|------|--------|
| `apps/agents/mcp_client.py` | Drop singleton; accept `on_progress` param |
| `apps/chat/views.py` | Create queue + callback; pass to stream |
| `apps/chat/stream.py` | Early card open; concurrent queue/event loop |
| `tests/test_chat_stream.py` | Unit tests for progress path |
