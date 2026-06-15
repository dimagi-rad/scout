# Gap round 1 — MCP tool output wire contract: server → LangChain → stream/converter → frontend cards

Reviewer: gap1-seam-mcp-tool-output-contract
Date: 2026-06-12
Scope: the 10 MCP tools other than `run_materialization` (`list_tables`, `describe_table`,
`get_metadata`, `get_lineage`, `query`, `list_pipelines`, `get_materialization_status`,
`cancel_materialization`, `get_schema_status`, `teardown_schema`), traced producer → wire →
both frontend ingestion paths (live SSE and thread-reload), diffed against the hand-written
TS interfaces in `ToolOutput.tsx` and the parsing in `ChatMessage.tsx`.

Known findings already in the database are NOT re-reported (notably: "live stream sends empty
tool input and 2000-char-truncated unparseable output / apostrophe-replace", "live-stream
toolCallId mismatch", "chat stream protocol swallows errors", "get_schema_status reads an
extinct result shape", "get_metadata returns 0 tables for multi-tenant" — all backend-side).
This report supplies the per-tool breakdown that finding lacked, plus several new defects.

---

## 1. The actual wire pipeline (verified against installed library versions)

Versions from `uv.lock` / `frontend/package.json`: `mcp 1.26.0`, `langchain-mcp-adapters 0.2.1`,
`langgraph 1.0.10`, `langchain-core 1.2.17`, frontend `ai ^6.0.78`.

**Producer.** Every tool returns a Python dict envelope from `mcp_server/envelope.py`:
success = `{"success": true, "data": {...}, "schema": "<schema>", "timing_ms"?, "warnings"?,
"project_id"?}` (`envelope.py:34-54`); error = `{"success": false, "error": {"code", "message",
"detail"?}}` (`envelope.py:57-67`).

**FastMCP serialization.** All 10 tools are annotated `-> dict` (bare). Verified empirically:
`func_metadata(f)` for a `-> dict` function yields `output_schema: None`, so **no
structuredContent** is produced. The unstructured path serializes the dict as
**indent-2 pretty-printed JSON** — `mcp/server/fastmcp/utilities/func_metadata.py:531`:

```python
result = pydantic_core.to_json(result, fallback=str, indent=2).decode()
return [TextContent(type="text", text=result)]
```

This is double-quoted JSON, never Python repr.

**LangChain adapter.** `langchain_mcp_adapters/tools.py:_convert_call_tool_result`
(`tools.py:176-180` in 0.2.1) converts each `TextContent` to a LangChain text block via
`create_text_block` → `{"type": "text", "text": "<indent-2 JSON>", "id": "lc_<uuid>"}`
(`langchain_core/messages/content.py:978-982`). The ToolMessage content is therefore a
**list of dict blocks**, artifact `None` (no structuredContent). `MultiServerMCPClient`
defaults `tool_name_prefix=False` (`client.py:57`), so tool names arrive unprefixed and match
the frontend's `switch (toolName)` cases.

**Live path** (`apps/chat/stream.py`). At `on_tool_end` only (`stream.py:159`), the backend
emits **both** `tool-input-available` with `"input": {}` (`stream.py:191-198`, known finding)
and `tool-output-available` whose `output` is `_tool_content_to_str(ToolMessage)` —
the text block re-pretty-printed with `json.dumps(parsed, indent=2)` (`stream.py:47-73`) —
then truncated:

```python
truncated = len(content) > 2000
display_content = content[:2000]
if truncated:
    display_content += f"\n\n... (truncated, {len(content)} chars total)"
```

(`stream.py:200-203`). A truncated pretty JSON string is never parseable.
`toolCallId` is the LangGraph `run_id`, not the LLM `tool_call_id` (`stream.py:190`, known).

**Reload path** (`apps/chat/thread_views.py:131-159` → `apps/chat/message_converter.py`).
`thread_messages_view` loads checkpointer messages and emits parts
`{"type": "tool-<name>", "toolCallId": tc["id"], "input": tc["args"], "output":
_tool_content_to_str(tr), "state": "output-available"}` (`message_converter.py:56-71`).
Output is the **full untruncated** pretty JSON; `input` is the **real LLM args** —
verified that the injected context IDs (`workspace_id`, `user_id`, `thread_id`,
`tool_call_id`) do *not* leak into reload input, because `_make_injecting_tool_node`
modifies a copy of the AIMessage and never persists it
(`apps/agents/graph/base.py:455-475`: `modified_msg` is fed to `base_tool_node.ainvoke`
but only the resulting ToolMessages are returned as state updates).

**Frontend ingestion.** ai SDK v6 `tool-input-available`/`tool-output-available` chunks create
parts of type `tool-${toolName}` (verified in `frontend/node_modules/ai/dist/index.mjs:5297-5318`).
`ChatMessage.tsx:parseOutput` (lines 24-53): for string output it (a) replaces **all**
apostrophes with double quotes and tries to parse a `[{type:'text',text}]` array, (b) falls
back to `JSON.parse(output)`, (c) falls back to the raw string. `renderToolOutput`
(lines 55-71) dispatches rich cards for exactly four tools: `query`, `describe_table`,
`list_tables`, `get_metadata`; everything else gets the `<pre>` fallback, which is
`fallbackText.slice(0, 2000)` (`ChatMessage.tsx:300-303`).

A consequence of the version trace: **the apostrophe-replace branch is vestigial under the
current stack.** FastMCP emits double-quoted JSON; `_tool_content_to_str` joins block texts
into one pretty JSON document; branch (a) parses it into a non-array object, fails the
`Array.isArray` check, and falls through harmlessly to branch (b), which is what actually
does the work. The comment "MCP wraps results as [{'type':'text'...}] with single quotes"
describes a shape no current backend path emits (it could only match legacy checkpoints
written by a pre-0.2 adapter/stream that `str()`-ed content). The already-known finding's
mechanism statement ("rendering hinges on the apostrophe replace") is therefore inaccurate
today — rendering hinges on `JSON.parse` of the pretty-printed envelope, and what breaks it
live is solely the 2000-char truncation.

---

## 2. Per-tool matrix: broken live, on reload, or both

Concrete size thresholds were measured by simulating the exact serialization
(`json.dumps(envelope, indent=2)`):

| payload | chars | vs 2000 |
|---|---|---|
| list_tables, 6 tables | 1,740 | ok |
| list_tables, 8 tables | 2,280 | truncated |
| describe_table, 8 columns | 1,476 | ok |
| describe_table, 12 columns | 2,134 | truncated |
| query, 10 rows × 5 short cols | 1,592 | ok |
| get_metadata, 4 tables × 10 cols | 7,856 | truncated |

| Tool | Rich card | Live (stream) | Reload (converter) | Verdict |
|---|---|---|---|---|
| `query` | yes | Rich card only for small results (≈≤10 rows of short values). Larger results → unparseable truncated JSON in a `<pre>`. Error envelopes are small → **error card works live**. | Rich card always works (full string). | broken-live-for-typical-success, fine on reload |
| `describe_table` | yes | Breaks at ≈11+ columns — most real tables (raw_cases/raw_forms) exceed this. | Works. | broken-live-typical, fine on reload |
| `list_tables` | yes | Breaks at ≈7+ tables. Multi-source pipelines (Connect ~5 sources + dbt models) sit at/over the line. | Works. | broken-live-borderline, fine on reload |
| `get_metadata` | yes | **Always** truncated (full snapshot ≫2000) → raw text. | Parses, but the card **always renders "0 tables"** (see Finding 1). | **broken on both paths, differently** |
| `get_lineage` | no | raw JSON, stream-truncated with explicit marker | raw JSON, silently sliced to 2000 (no marker) | no card by design |
| `list_pipelines` | no | same | same | no card |
| `get_materialization_status` | no | same; `result` field can be large (per-source error dicts) | same | no card |
| `cancel_materialization` | no | small ack, fine | fine | no card |
| `get_schema_status` | no | small unless `tables` long | same | no card; producer-side shape drift (Finding 5) |
| `teardown_schema` | no | small ack, fine | fine | no card |

Systematic live-vs-reload disagreements for the *same* tool call:

1. `input`: `{}` live vs real LLM args on reload (known finding — confirmed at
   `stream.py:196` vs `message_converter.py:61`).
2. `toolCallId`: LangGraph `run_id` live vs Anthropic `toolu_…` on reload (known finding).
3. `output`: 2000-truncated+marker live vs full string on reload — so the rich cards for
   `query`/`describe_table`/`list_tables` typically appear **only after the user leaves and
   re-opens the thread** (or after the background-completion `messageReloadKey` refetch in
   `ChatPanel.tsx:120-131`).
4. Reasoning/thinking parts exist live but are dropped entirely on reload (Finding 4).
5. `state: "output-error"` is producible by neither path; `ToolCallPart`'s
   `part.state === "output-error"` check (`ChatMessage.tsx:154`) is dead code. Hard tool
   failures (FastMCP `isError=True` → adapter `ToolException` → ToolNode's default
   `handle_tool_errors` ToolMessage) arrive as plain non-JSON strings rendered through the
   fallback on both paths.

---

## 3. Findings

### Finding 1 — `get_metadata` card renders "0 tables" on every successful reload: frontend counts `Array.isArray` over what is actually an object map
- **Status:** BROKEN-NOW · **Impact:** correctness · **Confidence:** verified-by-trace · **Complexity:** accidental
- **Chain:**
  - `mcp_server/services/metadata.py:342-346` — `tables = {}` … `tables[t["name"]] = detail` → `{"tables": <dict>, "relationships": [...]}`
  - `mcp_server/server.py:276-285` — `success_response({"schema": …, "table_count": len(metadata["tables"]), "tables": metadata["tables"], "relationships": …})` — `data.tables` is a **dict keyed by table name**; a correct scalar `table_count` is sent alongside.
  - serialized verbatim (FastMCP `func_metadata.py:531`), checkpointed, returned by `thread_views.py:158` → `message_converter.py:69` as a full JSON string.
  - `ChatMessage.tsx:34-35` — `JSON.parse(output)` succeeds → envelope object.
  - `ChatMessage.tsx:66-67` — `case "get_metadata": return <GetMetadataOutputComponent …>`.
  - `ToolOutput.tsx:313` — `const tableCount = Array.isArray(output.data.tables) ? output.data.tables.length : 0` → `Array.isArray({...})` is `false` → **0, always**.
  - `ToolOutput.tsx:318` — `<Badge variant="muted">{tableCount} tables</Badge>` renders "Metadata loaded · 0 tables" with a green check, for a snapshot that contains tables.
- The TS interface codifies the error: `GetMetadataOutput.data: { tables: unknown[] }`
  (`ToolOutput.tsx:303-308`) vs runtime `Record<string, {name, description, columns}>` plus
  `schema`, `table_count`, `relationships` which the type omits. The component ignores the
  `table_count` field that would have been correct.
- **Reachability:** `get_metadata` is in `MCP_TOOL_NAMES` (`graph/base.py:65-77`), exposed to
  the LLM, and in `AUTO_EXPAND_TOOLS` (`ChatMessage.tsx:130-136`). On the live path the card is
  unreachable anyway (payload always >2000 chars → raw text), so every time the rich card *is*
  reached (thread reload, shared thread page), it shows the wrong count. In multi-tenant
  workspaces the backend genuinely returns 0 tables (separate known finding), so there the lie
  is masked; single-tenant with data is where it shows.

### Finding 2 — Per-tool live/reload split: rich cards for query/describe_table/list_tables fail live for typical payloads and succeed on reload; get_metadata fails on both *(refines the already-known truncation finding — per-tool breakdown requested by this gap round)*
- **Status:** BROKEN-NOW · **Impact:** correctness (UI) · **Confidence:** verified-by-trace · **Complexity:** accidental
- Mechanism and measured thresholds in §2. Chain: `stream.py:200-210` (truncate + suffix) →
  `ChatMessage.tsx:24-53` (`JSON.parse` of truncated JSON fails → raw string) →
  `renderToolOutput` returns null (`output` not an object) → `<pre>` fallback of mangled JSON.
  The identical tool call renders a correct rich card after reload because
  `message_converter.py:69` sends the full string.
- Net effect: during a live session the four auto-expanded "rich" tools usually show a wall of
  truncated JSON; error envelopes (small) ironically render *better* live than successes.
- Also note the double cost: FastMCP already pretty-prints (indent=2, `func_metadata.py:531`)
  and `_try_pretty_json` re-parses and re-dumps at indent 2 (`stream.py:65-73`) — the
  pretty-printing that bloats payloads past 2000 chars is applied twice for no benefit, and the
  2000 budget is spent mostly on indentation whitespace (a compact `json.dumps` of the 8-table
  list_tables payload is ~40% smaller).

### Finding 3 — Error envelope information discarded by three rich cards; their TS types omit `error` entirely
- **Status:** DEBT · **Impact:** velocity (debuggability) · **Confidence:** verified-by-trace · **Complexity:** accidental
- Runtime errors are `{"success": false, "error": {"code", "message", "detail"?}}`
  (`envelope.py:57-67`; e.g. describe_table NOT_FOUND `server.py:217-221`, query errors from
  `services/query.py:96-145`).
- `DescribeTableOutput`, `ListTablesOutput`, `GetMetadataOutput` interfaces
  (`ToolOutput.tsx:170-184, 247-260, 303-308`) have **no `error` field**, and their components
  render only a generic string: "Failed to describe table" / "Failed to list tables" /
  "Failed to get metadata" (`ToolOutput.tsx:188, 264, 312`) — `error.code` and `error.message`
  (e.g. *"Table 'x' not found in schema 'y'"*) are parsed and thrown away.
- `QueryOutput.error` is typed `{ code: string; message: string }` (`ToolOutput.tsx:41`) —
  stricter than runtime, which may include `detail` (`envelope.py:64-66`); never displayed.

### Finding 4 — Thread reload silently drops reasoning/thinking parts: converter emits only text and tool parts
- **Status:** DEBT · **Impact:** correctness (UI parity) · **Confidence:** verified-by-trace · **Complexity:** accidental
- Live: `stream.py:138-146` emits `reasoning-start/delta/end`; `ChatMessage.tsx:385-387`
  renders a collapsible "Thinking" block.
- Reload: `message_converter.py:39-53` extracts only `type == "text"` blocks from AIMessage
  content (thinking blocks fail the filter at line 50 and contribute `""`), and no
  `{"type": "reasoning"}` part is ever emitted. After any reload — including the automatic
  post-materialization `messageReloadKey` refetch that replaces the in-memory messages array
  (`ChatPanel.tsx:96-97, 126-131`) — all Thinking blocks vanish from the transcript.
- May be semi-intentional (historical reasoning is collapsed anyway), but it is an
  undocumented live/reload divergence in the same seam, and the post-job auto-reload makes it
  user-visible mid-session.

### Finding 5 — `get_schema_status` envelope self-inconsistency at the producer: `timing_ms` present on 0 of 5 success branches; FAILED variant moves the error inside `data` as a bare string
- **Status:** DEBT · **Impact:** velocity (contract drift) · **Confidence:** verified-by-trace · **Complexity:** accidental
- Every other tool passes `timing_ms=tc["timer"].elapsed_ms`; all five `get_schema_status`
  success branches omit it (`server.py:667-675, 726-735, 754-764, 789-798`).
- The FAILED-view-schema variant returns `success: true` with
  `data: {"exists": true, "state": "failed", "error": "<string>", …}` (`server.py:754-764`) —
  the only place in the API where an error is a string inside `data` rather than the top-level
  `{"error": {code, message}}` object. Today only the LLM consumes this tool (no rich card),
  but any future card or programmatic consumer must special-case it; it also means generic
  "is this an error?" checks (`success == false`, top-level `error`) classify a failed build
  as success — the same class of drift that produced the known "agent told 'completed'"
  incident.

### Finding 6 — Reload fallback display silently slices to 2000 chars even though the full output is in memory
- **Status:** COSMETIC · **Impact:** velocity · **Confidence:** verified-by-trace · **Complexity:** accidental
- `ChatMessage.tsx:300-303`: `{fallbackText.slice(0, 2000)}` — no truncation marker (the live
  path at least appends "... (truncated, N chars total)", `stream.py:202-203`). Affects all six
  card-less tools plus any rich-tool payload that fails to parse. The data is already
  client-side; the cap is display-only and unmarked.

### Finding 7 — Live tool cards appear only after the tool finishes: `tool-input-available` is emitted at `on_tool_end`, so the AI-SDK input-streaming/input-available lifecycle is unused
- **Status:** DEBT · **Impact:** velocity (UX) · **Confidence:** verified-by-trace · **Complexity:** accidental
- `stream.py` has no `on_tool_start` handler; both tool chunks are emitted back-to-back inside
  the `on_tool_end` branch (`stream.py:159-210`). During a long `query` (statement timeout up
  to 30s) or `describe_table`, the live UI shows no tool card at all; `isLoading`
  (`ChatMessage.tsx:153`, `state === "input-streaming" || "input-available"`) can never be true
  on the live path because input and output arrive in the same tick. Adjacent to (but distinct
  from) the known "empty tool input" finding: this is the *timing* half of the same contract
  violation.

### Finding 8 — Remaining TS-vs-runtime diffs (looser/stricter inventory)
- **Status:** COSMETIC · **Impact:** velocity · **Confidence:** verified-by-trace · **Complexity:** accidental
- `ListTablesOutput.data.note?: string` vs runtime `string | null` (`server.py:133, 163-169`) —
  works only because the falsy check at `ToolOutput.tsx:278` tolerates null.
- `ListTablesOutput` table entries omit `materialized_at` (always sent,
  `metadata.py:85-94, 175-185`); `DescribeTableOutput` columns omit `default` (always sent,
  `metadata.py:218-226`) — looser-than-runtime, benign.
- `QueryOutput.data.rows: unknown[][]` — runtime cells can be objects/arrays (JSONB columns,
  `query.py:57`); `String(cell)` at `ToolOutput.tsx:139` renders `[object Object]`.
- `success` envelope fields `project_id` and `warnings` exist at runtime for all tools
  (`envelope.py:48-51`); only `QueryOutput` types `warnings`; no type carries `project_id`.
- Six of ten tools have no TS contract at all (fallback rendering): `get_lineage`,
  `list_pipelines`, `get_materialization_status`, `cancel_materialization`,
  `get_schema_status`, `teardown_schema`.
- `parseOutput`'s apostrophe-replace branch (`ChatMessage.tsx:26-32`) is vestigial under
  mcp 1.26 / adapter 0.2.1 (see §1): for current backend payloads it can never return (parsed
  value is a non-array object) and real parsing happens in the second `JSON.parse`. It remains
  a latent mis-parse hazard only for hypothetical outputs that are JSON arrays whose first
  element has `.text`, and possibly serves checkpoints written by older library versions.

---

## 4. What's fine (verified healthy)

- **Producer envelope discipline**: all 10 tools route through
  `success_response`/`error_response`; no tool hand-rolls a divergent top-level shape
  (the one drift is inside `get_schema_status.data`, Finding 5).
- **Serialization chain is clean JSON end-to-end** on current versions: FastMCP indent-2
  double-quoted JSON → adapter text blocks → checkpointer → converter. No Python-repr leakage
  on any current path.
- **Reload pairing logic**: `message_converter.py` pairing of `AIMessage.tool_calls` to
  `ToolMessage.tool_call_id` is correct, and `state` falls back to `input-available` for
  unpaired calls.
- **No context-ID leakage to the UI**: injected `workspace_id`/`user_id`/`thread_id`/
  `tool_call_id` never appear in reload `input` because the injecting node doesn't persist the
  modified AIMessage (`graph/base.py:455-475`).
- **Tool naming**: `MultiServerMCPClient` default `tool_name_prefix=False` keeps names aligned
  with the frontend `switch` and `MCP_TOOL_NAMES`.
- **No structured-content double-payload**: bare `-> dict` annotations mean
  `output_schema=None`, so checkpoints don't store the envelope twice (content + artifact).
- **`thread_messages_view` scoping**: ownership + workspace scoping with the deliberate
  404-vs-empty-list distinction (`thread_views.py:146-156`) is coherent.
- **ai SDK chunk ingestion**: `tool-input-available`/`tool-output-available` handling creates
  `tool-${name}` parts exactly as the converter's hand-built parts do, so the two ingestion
  paths converge on the same part type.

## 5. Coverage log

**Deep-read:** `mcp_server/server.py` (all 982 lines), `mcp_server/envelope.py`,
`mcp_server/services/metadata.py`, `mcp_server/services/query.py`, `apps/chat/stream.py`,
`apps/chat/message_converter.py`, `apps/chat/thread_views.py`,
`frontend/src/components/ChatMessage/ToolOutput.tsx` (all, including 120+),
`frontend/src/components/ChatMessage/ChatMessage.tsx` (all),
`apps/agents/graph/base.py` lines 1-130 and 396-545, `apps/agents/mcp_client.py`,
vendored `langchain_mcp_adapters/tools.py` (conversion paths),
vendored `mcp/server/fastmcp/utilities/func_metadata.py` (convert_result/_convert_to_content),
vendored `langchain_core/messages/content.py` (create_text_block),
vendored `ai/dist/index.mjs` (tool-chunk handling, spot-read).

**Skimmed:** `frontend/src/components/ChatPanel/ChatPanel.tsx` (lines 40-140),
`docs/arch-review/2026-06-12/cartography.md`, `langgraph/prebuilt/tool_node.py` (grep only),
`uv.lock` / `frontend/package.json` (versions).

**Not examined:** `apps/chat/views.py` (chat POST handler), checkpointer serde round-trip
against a real DB (content-shape survival asserted from JSON-serde reasoning, not observed),
public/shared thread page components (assumed to reuse ChatMessage), ai SDK zod acceptance of
the backend's non-standard `finish` chunk fields, exact ToolNode `handle_tool_errors` template
text in langgraph-prebuilt 1.0.8, legacy checkpoint content shapes in production data
(pre-adapter-0.2 threads), `tests/qa/` scenarios covering tool cards, `SqlHighlighter.tsx`,
`run_materialization` card behavior (excluded by mandate), recipes/widget surfaces that may
also render tool parts.
