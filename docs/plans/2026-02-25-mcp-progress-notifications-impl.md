# MCP Progress Notifications Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Surface `run_materialization` step progress into the frontend tool card in real time.

**Architecture:** The MCP server already emits `ctx.report_progress()` at each pipeline step. `langchain-mcp-adapters` supports an `on_progress` async callback via `Callbacks(on_progress=...)` at client construction. We create a per-request `MultiServerMCPClient` with that callback, bridge progress events to the SSE stream via an `asyncio.Queue`, and use `asyncio.wait(FIRST_COMPLETED)` in `langgraph_to_ui_stream` so progress items can be yielded while awaiting the next LangGraph event.

**Tech Stack:** Python asyncio, `langchain-mcp-adapters` `Callbacks`/`ProgressCallback`, Django SSE stream, pytest-asyncio.

---

### Task 1: Drop the MCP client singleton; create per-request clients with optional `on_progress`

**Files:**
- Modify: `apps/agents/mcp_client.py`
- Modify: `tests/test_mcp_client.py`

The singleton `_mcp_client` / `_mcp_lock` and the `get_mcp_client()` function are removed.
`get_mcp_tools()` creates a fresh `MultiServerMCPClient` on every call, passing the optional
callback. The circuit breaker module-level counters stay unchanged.

**Step 1: Update the tests first**

Replace the `TestMCPClient` class in `tests/test_mcp_client.py` (lines 235–271):

```python
class TestMCPClient:
    @pytest.mark.asyncio
    async def test_get_mcp_tools_returns_tools(self):
        """get_mcp_tools creates a client and returns its tools."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_tool = AsyncMock()
        mock_tool.name = "query"
        mock_client.get_tools.return_value = [mock_tool]

        with patch("apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client):
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                tools = await mod.get_mcp_tools()

        assert len(tools) == 1
        assert tools[0].name == "query"
        mock_client.get_tools.assert_awaited_once()
        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_creates_new_client_each_call(self):
        """get_mcp_tools creates a fresh client on each call (no singleton)."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_client.get_tools.return_value = []

        with patch("apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client) as MockCls:
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                await mod.get_mcp_tools()
                await mod.get_mcp_tools()

        assert MockCls.call_count == 2
        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_passes_callback_to_client(self):
        """on_progress callback is forwarded to Callbacks(on_progress=...)."""
        import apps.agents.mcp_client as mod
        from langchain_mcp_adapters.callbacks import Callbacks

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_client.get_tools.return_value = []

        async def my_callback(progress, total, message, context):
            pass

        with patch("apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client) as MockCls:
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                await mod.get_mcp_tools(on_progress=my_callback)

        _, kwargs = MockCls.call_args
        assert isinstance(kwargs.get("callbacks"), Callbacks)
        assert kwargs["callbacks"].on_progress is my_callback
        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_get_mcp_tools_no_callback_passes_none(self):
        """Without on_progress, callbacks kwarg is None."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        mock_client = AsyncMock()
        mock_client.get_tools.return_value = []

        with patch("apps.agents.mcp_client.MultiServerMCPClient", return_value=mock_client) as MockCls:
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                await mod.get_mcp_tools()

        _, kwargs = MockCls.call_args
        assert kwargs.get("callbacks") is None
        mod.reset_circuit_breaker()

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_failures(self):
        """Circuit breaker raises MCPServerUnavailable after threshold failures."""
        import apps.agents.mcp_client as mod

        mod.reset_circuit_breaker()

        with patch("apps.agents.mcp_client.MultiServerMCPClient", side_effect=Exception("down")):
            with patch.object(mod, "settings") as mock_settings:
                mock_settings.MCP_SERVER_URL = "http://localhost:8100/mcp"
                for _ in range(mod._CIRCUIT_BREAKER_THRESHOLD):
                    with pytest.raises(Exception):
                        await mod.get_mcp_tools()

                with pytest.raises(mod.MCPServerUnavailable):
                    await mod.get_mcp_tools()

        mod.reset_circuit_breaker()
```

**Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_mcp_client.py::TestMCPClient -v
```

Expected: failures on the new tests (old singleton test is gone; new tests reference the new API).

**Step 3: Rewrite `apps/agents/mcp_client.py`**

Replace the entire file:

```python
"""
MCP client for connecting the Scout agent to the MCP data server.

Creates a fresh MultiServerMCPClient per call so per-request progress
callbacks can be attached. Circuit breaker logic prevents hammering an
unavailable server.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.conf import settings
from langchain_mcp_adapters.callbacks import Callbacks, CallbackContext
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

# Circuit breaker state
_consecutive_failures: int = 0
_last_failure_time: float = 0.0
_CIRCUIT_BREAKER_THRESHOLD = 5
_CIRCUIT_BREAKER_COOLDOWN = 30.0

# Type alias matching langchain_mcp_adapters.callbacks.ProgressCallback
ProgressCallback = Callable[
    [float, float | None, str | None, CallbackContext],
    Coroutine[Any, Any, None],
]


class MCPServerUnavailable(Exception):
    """Raised when the circuit breaker is open."""


async def get_mcp_tools(on_progress: ProgressCallback | None = None) -> list:
    """Load MCP tools as LangChain tools.

    Creates a fresh MultiServerMCPClient on each call. Pass on_progress to
    receive real-time step updates during long-running tools such as
    run_materialization.

    Raises MCPServerUnavailable when the circuit breaker is open.
    """
    global _consecutive_failures, _last_failure_time

    if _consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
        elapsed = time.monotonic() - _last_failure_time
        if elapsed < _CIRCUIT_BREAKER_COOLDOWN:
            raise MCPServerUnavailable(
                f"MCP server circuit breaker open ({_consecutive_failures} consecutive failures). "
                f"Retry in {_CIRCUIT_BREAKER_COOLDOWN - elapsed:.0f}s."
            )
        logger.info("Circuit breaker cooldown elapsed, allowing retry")

    url = settings.MCP_SERVER_URL
    callbacks = Callbacks(on_progress=on_progress) if on_progress else None
    try:
        client = MultiServerMCPClient(
            {"scout-data": {"transport": "streamable_http", "url": url}},
            callbacks=callbacks,
        )
        tools = await client.get_tools()
        logger.info("Loaded %d MCP tools: %s", len(tools), [t.name for t in tools])
        _consecutive_failures = 0
        return tools
    except MCPServerUnavailable:
        raise
    except Exception:
        _consecutive_failures += 1
        _last_failure_time = time.monotonic()
        logger.error("MCP tool loading failed (attempt %d)", _consecutive_failures)
        raise


def reset_circuit_breaker() -> None:
    """Reset circuit breaker state. Used in tests."""
    global _consecutive_failures, _last_failure_time
    _consecutive_failures = 0
    _last_failure_time = 0.0


# --- OAuth token retrieval ---

COMMCARE_PROVIDERS = frozenset({"commcare", "commcare_connect"})


async def get_user_oauth_tokens(user) -> dict[str, str]:
    """Retrieve OAuth tokens for a user's CommCare providers."""
    if user is None or not getattr(user, "pk", None):
        return {}
    return await sync_to_async(_get_tokens_sync)(user)


def _get_tokens_sync(user) -> dict[str, str]:
    social_tokens = SocialToken.objects.filter(
        account__user=user,
        account__provider__in=COMMCARE_PROVIDERS,
    ).select_related("account")
    return {
        st.account.provider: st.token
        for st in social_tokens
        if st.account.provider in COMMCARE_PROVIDERS
    }
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_mcp_client.py -v
```

Expected: all pass.

**Step 5: Commit**

```bash
git add apps/agents/mcp_client.py tests/test_mcp_client.py
git commit -m "refactor: drop MCP client singleton, create per-request clients with optional on_progress"
```

---

### Task 2: Refactor `langgraph_to_ui_stream` to support a progress queue

**Files:**
- Modify: `apps/chat/stream.py`
- Modify: `tests/test_mcp_chat_integration.py`

Replace the `async for` event loop with a unified `asyncio.wait(FIRST_COMPLETED)` loop.
When no `progress_queue` is supplied, a noop empty queue is used so the code path is
identical. Add `on_tool_start` handling that opens the tool card early for
`run_materialization` and tracks its `toolCallId` for progress updates.

**Step 1: Write failing tests**

Add a new `TestProgressStream` class to `tests/test_mcp_chat_integration.py`:

```python
class TestProgressStream:
    """Tests for progress queue integration in langgraph_to_ui_stream."""

    @pytest.mark.asyncio
    async def test_run_materialization_card_opens_on_tool_start(self):
        """on_tool_start for run_materialization should emit tool-input-available immediately."""
        mock_agent = AsyncMock()

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_tool_start",
                "run_id": "run-mat-1",
                "name": "run_materialization",
                "data": {},
            }
            yield {
                "event": "on_tool_end",
                "run_id": "run-mat-1",
                "name": "run_materialization",
                "data": {
                    "output": ToolMessage(
                        content='{"success": true, "status": "completed"}',
                        tool_call_id="call-mat-1",
                        name="run_materialization",
                    ),
                },
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        types = [e["type"] for e in events]
        # tool-input-available must appear (opened on tool_start)
        assert "tool-input-available" in types
        # tool-output-available must appear (final result on tool_end)
        assert "tool-output-available" in types
        # Only one tool-input-available (not duplicated)
        assert types.count("tool-input-available") == 1

    @pytest.mark.asyncio
    async def test_progress_queue_items_emitted_as_tool_output(self):
        """Progress queue items should be emitted as tool-output-available with progress text."""
        import asyncio as _asyncio
        mock_agent = AsyncMock()

        progress_queue = _asyncio.Queue()

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_tool_start",
                "run_id": "run-mat-2",
                "name": "run_materialization",
                "data": {},
            }
            # Simulate progress arriving while tool runs
            await progress_queue.put({"current": 1, "total": 3, "message": "Discovering metadata"})
            await progress_queue.put({"current": 2, "total": 3, "message": "Loading cases"})
            yield {
                "event": "on_tool_end",
                "run_id": "run-mat-2",
                "name": "run_materialization",
                "data": {
                    "output": ToolMessage(
                        content='{"success": true, "rows_loaded": 42}',
                        tool_call_id="call-mat-2",
                        name="run_materialization",
                    ),
                },
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}, progress_queue=progress_queue):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        tool_outputs = [e for e in events if e["type"] == "tool-output-available"]
        # At least the two progress updates + the final result
        assert len(tool_outputs) >= 3
        # Progress text contains the message
        assert any("Discovering metadata" in e.get("output", "") for e in tool_outputs)
        assert any("Loading cases" in e.get("output", "") for e in tool_outputs)
        # All updates share the same toolCallId
        call_ids = {e["toolCallId"] for e in tool_outputs}
        assert len(call_ids) == 1

    @pytest.mark.asyncio
    async def test_final_result_replaces_progress(self):
        """The on_tool_end result is the last tool-output-available."""
        import asyncio as _asyncio
        mock_agent = AsyncMock()

        progress_queue = _asyncio.Queue()

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_tool_start",
                "run_id": "run-mat-3",
                "name": "run_materialization",
                "data": {},
            }
            await progress_queue.put({"current": 1, "total": 2, "message": "Step one"})
            yield {
                "event": "on_tool_end",
                "run_id": "run-mat-3",
                "name": "run_materialization",
                "data": {
                    "output": ToolMessage(
                        content='{"success": true, "status": "completed", "rows_loaded": 100}',
                        tool_call_id="call-mat-3",
                        name="run_materialization",
                    ),
                },
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}, progress_queue=progress_queue):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        tool_outputs = [e for e in events if e["type"] == "tool-output-available"]
        # Last output is the final result (contains rows_loaded)
        assert "rows_loaded" in tool_outputs[-1].get("output", "")

    @pytest.mark.asyncio
    async def test_non_materialization_tool_unaffected(self):
        """Other tools still use the on_tool_end-only path."""
        mock_agent = AsyncMock()

        async def fake_events(*args, **kwargs):
            yield {
                "event": "on_tool_end",
                "run_id": "run-query-1",
                "name": "query",
                "data": {
                    "output": ToolMessage(
                        content='{"success": true, "rows": []}',
                        tool_call_id="call-q-1",
                        name="query",
                    ),
                },
            }

        mock_agent.astream_events = fake_events

        events = []
        async for chunk in langgraph_to_ui_stream(mock_agent, {}, {}):
            for line in chunk.strip().split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        types = [e["type"] for e in events]
        assert "tool-input-available" in types
        assert "tool-output-available" in types
        assert types.count("tool-input-available") == 1
```

**Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_mcp_chat_integration.py::TestProgressStream -v
```

Expected: all 4 fail.

**Step 3: Rewrite `apps/chat/stream.py`**

Replace the entire file with the version below. The key changes are:
- New `_PROGRESS_TOOLS` sentinel and `_STREAM_DONE` sentinel
- `progress_queue` parameter (defaults to `None`; a noop queue is used when absent)
- `on_tool_start` handling for `_PROGRESS_TOOLS`
- Unified `asyncio.wait` loop replacing the `async for`

```python
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
        if isinstance(parsed, (dict, list)):
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
                    yield _sse({
                        "type": "tool-output-available",
                        "toolCallId": active_progress_tool_call_id,
                        "output": text,
                    })
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
                    yield _sse({
                        "type": "tool-input-available",
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "input": {},
                    })
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

                # Reuse the toolCallId if this card was pre-opened on tool_start.
                tool_call_id = tool_cards_opened.pop(run_id, None) if run_id else None
                if tool_call_id is None:
                    tool_call_id = run_id or uuid.uuid4().hex
                    yield _sse({
                        "type": "tool-input-available",
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "input": {},
                    })

                # Clear active progress tracker when the tool completes.
                if tool_call_id == active_progress_tool_call_id:
                    active_progress_tool_call_id = None

                truncated = len(content) > 2000
                display_content = content[:2000]
                if truncated:
                    display_content += f"\n\n... (truncated, {len(content)} chars total)"
                yield _sse({
                    "type": "tool-output-available",
                    "toolCallId": tool_call_id,
                    "output": display_content,
                })

    except TimeoutError:
        logger.warning("Agent execution timed out after %ds", AGENT_TIMEOUT_SECONDS)
        if reasoning_started:
            yield _sse({"type": "reasoning-end", "id": reasoning_id})
        if not text_started:
            yield _sse({"type": "text-start", "id": text_id})
            text_started = True
        yield _sse({
            "type": "text-delta",
            "id": text_id,
            "delta": "\n\nThe request timed out. Try simplifying your question or breaking it into smaller steps.",
        })
    except Exception:
        logger.exception("Error during agent streaming")
        if reasoning_started:
            yield _sse({"type": "reasoning-end", "id": reasoning_id})
        if not text_started:
            yield _sse({"type": "text-start", "id": text_id})
            text_started = True
        yield _sse({
            "type": "text-delta",
            "id": text_id,
            "delta": "\n\nAn error occurred while processing your request.",
        })

    # Close any open parts
    if reasoning_started:
        yield _sse({"type": "reasoning-end", "id": reasoning_id})
    if text_started:
        yield _sse({"type": "text-end", "id": text_id})

    # Finish markers
    yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish", "finishReason": "stop"})
```

**Step 4: Run new tests**

```bash
uv run pytest tests/test_mcp_chat_integration.py::TestProgressStream -v
```

Expected: all 4 pass.

**Step 5: Run full stream test suite to check for regressions**

```bash
uv run pytest tests/test_mcp_chat_integration.py::TestSSEStreamFormat -v
```

Expected: all pass.

**Step 6: Commit**

```bash
git add apps/chat/stream.py tests/test_mcp_chat_integration.py
git commit -m "feat: interleave MCP progress notifications in langgraph SSE stream"
```

---

### Task 3: Wire the queue and callback into the view

**Files:**
- Modify: `apps/chat/views.py`

**Step 1: Locate the relevant section in `views.py`**

Find the block starting at line ~525:
```python
    # Load MCP tools (data access via MCP server)
    try:
        mcp_tools = await get_mcp_tools()
```

And the `StreamingHttpResponse` call near line 586:
```python
    response = StreamingHttpResponse(
        langgraph_to_ui_stream(agent, input_state, config),
```

**Step 2: Apply the changes**

Replace the `get_mcp_tools()` block and `StreamingHttpResponse` call:

```python
    # Load MCP tools; attach progress callback for run_materialization updates.
    progress_queue: asyncio.Queue = asyncio.Queue()

    async def _on_mcp_progress(progress, total, message, context) -> None:
        if message is not None:
            await progress_queue.put({
                "current": int(progress),
                "total": int(total) if total else 0,
                "message": message,
            })

    try:
        mcp_tools = await get_mcp_tools(on_progress=_on_mcp_progress)
    except Exception as e:
        error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
        logger.exception("Failed to load MCP tools [ref=%s]", error_ref)
        return JsonResponse({"error": f"Agent initialization failed. Ref: {error_ref}"}, status=500)
```

And update the `StreamingHttpResponse`:

```python
    # Return streaming response (SSE for AI SDK v6 DefaultChatTransport)
    response = StreamingHttpResponse(
        langgraph_to_ui_stream(agent, input_state, config, progress_queue=progress_queue),
        content_type="text/event-stream; charset=utf-8",
    )
```

Also add `import asyncio` at the top of the file if not already present (check: it's not currently imported in views.py).

Add to the existing imports block:
```python
import asyncio
```

**Step 3: Run the full test suite**

```bash
uv run pytest tests/ -v --tb=short -q
```

Expected: all pass (no integration tests cover the progress path end-to-end, but existing tests should not regress).

**Step 4: Commit**

```bash
git add apps/chat/views.py
git commit -m "feat: wire MCP progress queue into chat view and SSE stream"
```

---

### Task 4: Final verification

**Step 1: Lint**

```bash
uv run ruff check apps/agents/mcp_client.py apps/chat/stream.py apps/chat/views.py
uv run ruff format --check apps/agents/mcp_client.py apps/chat/stream.py apps/chat/views.py
```

Fix any issues, then re-run.

**Step 2: Full test suite**

```bash
uv run pytest tests/ -q
```

Expected: all pass, no warnings about missing imports or changed interfaces.

**Step 3: Update TODO.md**

The `Cancellation support` item in `TODO.md` references progress notifications as a dependency. Add a note that progress notifications are now live. No checkbox changes needed for the cancellation item itself.
