# Lens report: Performance & Cost

*Reviewer: cross-cutting lens 8 (per-request rebuild work, unbounded growth, N+1, prompt/token
cost, polling load). Date: 2026-06-12. Repo HEAD 35e4230.*

Scope note: I hunted one defect class everywhere. I did not assess correctness, security, or
state-machine races except where they intersect cost. Evidence standards per
`docs/arch-review-methodology.md` — every finding carries status/impact/confidence and a
file:line chain.

---

## Summary

The hot path (chat turn → agent graph → MCP tools → managed DB) carries several layers of
per-request rebuild work that are individually tolerable and collectively expensive, and the
platform has **four unbounded-growth surfaces with no cleanup path at all**: LangGraph
checkpoints, procrastinate job/event rows, Thread rows, and artifact version rows. The single
largest *dollar* finding is that no Anthropic prompt caching is configured anywhere despite a
large, mostly-static system prompt and tool schema being re-billed on every LLM call of every
tool-loop iteration. The single largest *latency* finding is the per-call TLS connection churn
on the MCP data plane.

Nothing here is BROKEN-NOW; the class is "degrades with tenant/thread/knowledge count".

---

## Findings

### PC-1 — No Anthropic prompt caching anywhere; full system prompt + tool schemas re-billed every LLM call
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace (absence) ·
  **Complexity**: accidental
- **Chain**: `POST /api/chat/` (`apps/chat/views.py:65`) → `build_agent_graph`
  (`apps/agents/graph/base.py:517-522`) constructs `ChatAnthropic(model=..., max_tokens=4096)`
  with no cache hints → `agent_node` (`base.py:580-581`) prepends
  `SystemMessage(content=system_prompt)` and `await llm_with_tools.ainvoke(messages)` on **every
  graph cycle**. `grep -rn "cache_control|prompt_caching|ephemeral"` over `apps/`, `mcp_server/`,
  `config/` finds zero hits in agent code (only the dbt "ephemeral project" docstrings and an HTTP
  `cache_control` decorator in `config/views.py`).
- **Mechanism**: the system prompt is BASE_SYSTEM_PROMPT + ARTIFACT_PROMPT_ADDITION + workspace
  instructions + the entire knowledge base (PC-10) + up to 6,000 chars of schema context
  (`SCHEMA_CONTEXT_CHAR_BUDGET`, `base.py:81`), plus 11 MCP tool schemas + 4 local tool schemas.
  A turn with K tool calls makes K+1 LLM calls (recursion_limit 50, `chat/views.py:195`), each
  re-sending the identical system prompt, tool block, and ever-growing history at the full input
  token rate. Anthropic `cache_control` on the system block + tools would make every call after
  the first in a turn (and across turns within the cache TTL) pay cache-read rates (~10% of
  input) for that prefix. The system prompt is stable for 60s by construction
  (`_SYSTEM_PROMPT_TTL`, `base.py:128`), so cacheability is already engineered for — it's just
  never sent to the API.
- **Reachable via**: every chat message, every recipe run (`apps/recipes/services/runner.py:115`),
  every resume-after-materialization (`apps/workspaces/tasks.py:852-867`).

### PC-2 — Unbounded conversation history sent to the LLM; no trimming, summarization, or tool-result compaction
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: mixed (full-history agents are partly essential; *unbounded* is accidental)
- **Chain**: `agent_node` (`apps/agents/graph/base.py:545-581`) takes `list(state["messages"])`,
  filters SystemMessages, repairs dangling tool calls, and sends **everything** — there is no
  trim/window/summarize step anywhere in the graph. Tool results enter history verbatim: the
  `query` tool returns up to 500 rows of JSON (`mcp_server/context.py:26`
  `max_rows_per_query=500`; `mcp_server/services/query.py:138-145` returns full `rows`), which
  becomes a ToolMessage in the checkpoint and is re-sent on every subsequent LLM call of every
  subsequent turn, forever.
- **Mechanism**: per-turn input tokens grow linearly with thread age; cost of a thread grows
  ~quadratically over its life. A user who keeps one long-lived analysis thread (the product
  encourages this — threads resume across materializations) accrues per-message cost that can
  reach the model's context limit, at which point turns start failing (Anthropic 400) with no
  graceful degradation path in `stream.py` beyond the generic error text.
- **Interacts with**: PC-1 (no cache discount on the resent prefix) and PC-3 (the same growth is
  also persisted per-checkpoint).
- **Reachable via**: every chat turn on any thread older than a few turns.

### PC-3 — LangGraph checkpoints are never deleted or pruned; per-thread storage grows superlinearly, platform DB grows forever
- **Status**: LATENT · **Impact**: cost-perf · **Confidence**: verified-by-trace for "no cleanup
  exists"; strong-inference for the growth rate (library internals) · **Complexity**: accidental
- **Chain**: checkpoints are written by `AsyncPostgresSaver` (`apps/chat/checkpointer.py:40`,
  `ensure_checkpointer`) on every graph superstep of every turn. Deletion: there is **no thread
  delete endpoint** (`apps/chat/urls.py` exposes only list/messages/share/viewed) and no call to
  any checkpoint deletion API anywhere (`grep -rn "adelete_thread|delete_thread"` over `apps/`:
  zero non-test hits). `Thread` rows likewise have no deletion path.
- **Mechanism**: langgraph-checkpoint-postgres stores one checkpoint row per superstep plus
  channel blobs for every channel that changed; the `messages` channel changes every superstep,
  so each superstep persists a fresh serialized copy of the full message list. A turn with K tool
  calls writes ~2K+1 checkpoints; a thread of N messages has stored ~O(N²) message-copies. No
  TTL, no compaction, no `checkpoint_writes`/`checkpoint_blobs` vacuuming. The platform DB also
  hosts the procrastinate queue (PC-4) — the two growth curves share one RDS instance with the
  Django state tables, and `thread_messages_view` (`apps/chat/thread_views.py:99`) reads only the
  *latest* checkpoint, so 99% of this storage is write-only dead weight.
- **Reachable via**: every chat turn. Degrades with thread count × thread length × time.

### PC-4 — Procrastinate job/event/periodic-defer tables never pruned
- **Status**: LATENT · **Impact**: cost-perf · **Confidence**: strong-inference (retention
  default is library behavior; absence of cleanup verified) · **Complexity**: accidental
- **Chain**: worker runs plain `python manage.py procrastinate worker`
  (`config/deploy-worker.yml:16`; same in `Procfile.dev`) — no `--delete-jobs` / job retention
  flag. `grep -rn "remove_old_jobs|builtin_tasks"` over the repo: zero hits; the only periodic
  tasks are `expire_inactive_schemas` (`apps/workspaces/tasks.py:516`, */30) and
  `expire_stale_thread_jobs` (`tasks.py:819`, */15).
- **Mechanism**: procrastinate keeps finished jobs (`succeeded`/`failed`/`aborted`) in
  `procrastinate_jobs` plus rows in `procrastinate_events` and `procrastinate_periodic_defers`
  indefinitely unless `remove_old_jobs` (built-in maintenance task) is scheduled or the worker
  is told to delete on completion. The two janitors alone insert ~144 jobs/day; every
  materialization, teardown, sibling-rebuild and resume adds more, each with multiple event rows.
  The janitor/API backstop read path (`_procrastinate_job_status`, `tasks.py:693-725`) is a PK
  lookup and stays fast, but the queue's own `fetch_job` and the table/bloat footprint degrade
  with time, on the same platform DB as PC-3. Given this codebase has already had two
  worker/queue incidents (2026-05-30 zombie jobs, 2026-06-09 dead connection), an
  ever-growing queue table is asking for the third.
- **Reachable via**: all background work; growth is unconditional.

### PC-5 — MCP data plane opens a fresh TLS PostgreSQL connection for every query
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: accidental
- **Chain**: agent `query` tool → `execute_query` → `_execute_async`
  (`mcp_server/services/query.py:41`): `await psycopg.AsyncConnection.connect(**ctx.connection_params)`
  per call; same in `_execute_async_parameterized` (`query.py:73`). `connection_params` come from
  `_parse_db_url` (`mcp_server/context.py:142-163`) with `sslmode` defaulting to **"require"**
  (`context.py:161`) — i.e., a TCP + TLS + Postgres auth handshake per query. No pool exists
  anywhere in `mcp_server/`.
- **Per-call multipliers**: one connection per agent `query`/`describe_table` call; one per
  `list_tables` reconciliation (`_live_tables_in_schema`, `services/metadata.py:144-158`); one
  per table in `pipeline_get_metadata`'s describe loop (`metadata.py:344`); one per table in the
  system-prompt schema build (PC-6); one per `source_query` per artifact render (PC-12). A single
  "describe everything then query" agent turn against a 15-table schema can open 20+ fresh TLS
  connections serially.
- **Reachable via**: every data-touching agent tool call, artifact render, data-dictionary load.

### PC-6 — System-prompt schema context rebuilds with serial per-table round trips on every 60s cache miss, per user, per process
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: accidental
- **Chain**: `_build_system_prompt` (`apps/agents/graph/base.py:699-785`) caches per
  `(workspace, user, prompt-hash)` with a 60-second TTL (`base.py:127-128`) in a **per-process
  dict** → on miss, `_fetch_schema_context` (`base.py:205-296`) runs: TenantSchema lookup +
  terminal-assets query + `pipeline_list_tables` (which itself opens a fresh DB connection for
  `_live_tables_in_schema`) + `load_tenant_context` (another TenantSchema query **plus an
  `atouch()` write**, `mcp_server/context.py:56-66`) + a TenantMetadata query + **one
  `pipeline_describe_table` per table, serially** (`base.py:267-271`), each opening its own
  fresh TLS connection (PC-5).
- **Mechanism**: with T tables, U active users, and the production `uvicorn --workers 4`
  (`config/deploy.yml:17`) each holding an independent `_system_prompt_cache`, steady-state chat
  costs up to `4 × U × (T+4)` serial managed-DB round trips per minute per workspace — all on
  the latency-critical path before the first token streams. The 60s TTL means an actively
  chatting user pays this roughly once a minute.
- **Reachable via**: every chat turn (cache miss), every recipe run, every resume.

### PC-7 — Per-request MCP client construction + tools/list HTTP round trip; full agent-graph build even for one synthetic message
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: accidental
- **Chain**: `chat_view` → `get_mcp_tools()` (`apps/chat/views.py:155` →
  `apps/agents/mcp_client.py:31-56`): a fresh `MultiServerMCPClient` and a `tools/list` HTTP
  round trip to the MCP server **per chat message** ("Creates a fresh MultiServerMCPClient on
  each call" — the docstring justifies it for progress callbacks, but no per-request callback is
  attached at this call site). The same full build runs in `_build_agent_for_resume`
  (`apps/workspaces/tasks.py:852-867`), and `_persist_synthetic_failure_message`
  (`tasks.py:895-918`) builds the *entire* agent graph — MCP HTTP round trip, knowledge
  retrieval, schema-context fetch (PC-6), LLM binding — solely to call `aupdate_state` and
  append one fixed-text AIMessage.
- **Mechanism**: tool schemas are static between MCP deploys; rebuilding tools, re-binding
  `bind_tools`, recompiling the StateGraph per request is pure per-request rebuild work. It is
  also a hidden availability coupling: the janitor reconcile path can't write its failure
  message if the MCP server is down, despite needing nothing from it.
- **Reachable via**: every chat message; every reconcile that posts a synthetic message.

### PC-8 — Frontend polling: jobs endpoint every 3s per tab unconditionally; health every 5s; no idle backoff or visibility gating
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: accidental
- **Chain**: `useWorkspaceJobsImpl` (`frontend/src/hooks/useWorkspaceJobs.ts:4,86-90`):
  `setInterval(fetchOnce, 3000)` whenever a workspace is selected — it polls
  `GET /api/workspaces/<id>/jobs/active/` even when zero jobs exist, the thread is idle, and the
  tab is hidden (no `document.visibilityState` check, no backoff). Server side, each poll
  (`apps/workspaces/api/jobs_views.py:83-162`) runs: workspace resolution, the active-jobs query
  (re-run a second time if any reconcile fired, `jobs_views.py:135`), a MaterializationRun bulk
  fetch, and a recent-terminations query — ~5 queries per poll per tab, plus potential
  reconcile *writes* from the API process (`jobs_views.py:123-134`). `NetworkStatusContext.tsx:15`
  adds a `/health/` ping every 5s (cheap — `health_check` does no DB work,
  `apps/workspaces/views.py:8-13`).
- **Mechanism**: load is `(tabs × 0.33 rps × ~5 queries)` around the clock; with 100 open tabs
  that's ~165 queries/sec of steady-state idle load on the platform DB. The single-owner
  provider (`WorkspaceJobsContext.tsx`) already fixed duplicate polling within one tab; the
  remaining waste is the unconditional cadence.
- **Reachable via**: every open SPA tab with a selected workspace.

### PC-9 — Data dictionary view: per-table TableKnowledge N+1 plus two fresh managed-DB connections per request
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: accidental
- **Chain**: `GET /api/workspaces/<id>/data-dictionary/` → `DataDictionaryView._get_from_pipeline`
  (`apps/workspaces/api/views.py:252-311`): `pipeline_list_tables` (fresh connection via
  `_live_tables_in_schema`) + `_get_all_columns` (second fresh connection,
  `views.py:73-108`) + then **inside the per-table loop** `_get_annotation(workspace,
  qualified_name)` (`views.py:290` → `views.py:216-222`) issues one `TableKnowledge.objects.get`
  per table — a classic N+1 (one `filter(workspace=...)` prefetch would do). The view is sync
  DRF calling `async_to_sync(pipeline_list_tables)` (`views.py:275`), spinning an event loop per
  request.
- **Reachable via**: Data Dictionary page load (`frontend/src/store/dictionarySlice.ts:178`).

### PC-10 — Knowledge base is injected whole into the system prompt with no size cap
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: accidental
- **Chain**: `_build_system_prompt` → `KnowledgeRetriever.retrieve()`
  (`apps/knowledge/services/retriever.py:33-49`): `_format_knowledge_entries` iterates **all**
  `KnowledgeEntry` rows for the workspace with full `entry.content` (`retriever.py:51-66`, no
  limit), `_format_table_knowledge` iterates **all** `TableKnowledge` rows with all column notes
  (`retriever.py:68-113`, no limit); only learnings are capped (`MAX_AGENT_LEARNINGS = 20`,
  `retriever.py:28`). The knowledge app has an import endpoint
  (`apps/knowledge/api/views.py` import/export), so the table can grow large in one click.
- **Mechanism**: unlike the schema context, which has a 6,000-char budget and a compact
  fallback (`base.py:81,282-296`), the knowledge section is unbudgeted. Every entry is then
  re-billed on every LLM call (PC-1/PC-2). The retriever's `user_question` parameter
  (`retriever.py:33`) is accepted and ignored — relevance filtering was designed for and never
  implemented. Minor: each section runs `aexists()` then re-queries to iterate — 6 queries where
  3 would do.
- **Reachable via**: every chat turn in any workspace with knowledge entries.

### PC-11 — Per-process duplication under `--workers 4`: 4 caches, 4 circuit breakers, up to 80 checkpointer pool connections
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: strong-inference (per-process
  semantics traced; live connection counts not measured) · **Complexity**: accidental
- **Chain**: production API runs `uvicorn --workers 4` (`config/deploy.yml:17`). Module-level
  state is therefore quadrupled: `_system_prompt_cache` (`apps/agents/graph/base.py:127`), the
  MCP circuit breaker (`apps/agents/mcp_client.py:21-24`), and the checkpointer pool
  `AsyncConnectionPool(max_size=20)` (`apps/chat/checkpointer.py:29-38`) — up to 80 platform-DB
  connections from checkpointing alone, beside Django ORM connections and the worker. Cache
  hit rates divide by 4; the 60s TTL cost in PC-6 multiplies by 4.
- **Reachable via**: production deployment topology.

### PC-12 — Artifact rendering re-executes all source queries serially on every open, one fresh connection each
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: verified-by-trace ·
  **Complexity**: mixed (live data is a feature; serial fresh-connection execution and zero
  caching are accidental)
- **Chain**: `ArtifactQueryDataView.get` (`apps/artifacts/views.py:773-843`): for each entry in
  `artifact.source_queries`, `await execute_query(ctx, sql)` (`views.py:822`) — serial, each
  call opening a fresh TLS connection (PC-5), full SQL re-validation per query, no result
  caching or ETag. A dashboard artifact with 5 queries costs 5 sequential
  connect+validate+execute cycles per viewer per open.
- **Reachable via**: artifact panel, sandbox render, public shared pages that fetch query-data.

### PC-13 — Artifact versioning copies the full row per update; soft delete means no row ever leaves
- **Status**: DEBT · **Impact**: cost-perf · **Confidence**: strong-inference (model traced,
  agent-tool write path skimmed) · **Complexity**: mixed
- **Chain**: `Artifact.create_new_version` (`apps/artifacts/models.py:173-197`) creates a new
  row with the full `code` TextField and `data` JSON per update, linked by `parent_artifact`;
  deletion is soft (`SoftDeleteManager`, `models.py:16`) with an undelete endpoint, so rows are
  never physically removed. Agents iterate on artifacts (the `update_artifact` tool exists
  precisely for that), so an actively-edited dashboard accretes a full-source copy per
  iteration, forever.
- **Reachable via**: agent `update_artifact` tool on any artifact-producing thread.

---

## What's fine (verified healthy for this lens)

- **Workspace list endpoint** — `_schema_status_for_workspaces`
  (`apps/workspaces/api/workspace_views.py:63-106`) computes statuses with two bulk queries +
  prefetch; explicitly no N+1. The correlated `latest_run` subquery is annotated per membership,
  not looped.
- **Thread list** — capped at 50, single query (`apps/chat/thread_views.py:88-91`).
- **Materialization progress writes** — page-granularity, 2 queries per page
  (`apps/workspaces/tasks.py:485-494`); the updater doubles as the cancellation checkpoint, so
  the write frequency is load-bearing, not waste.
- **Sibling view-schema rebuild and dependent-view-failure queries** — both deliberately built as
  single annotated subqueries with in-code comments rejecting the N+1 form
  (`tasks.py:398-438,666-687`).
- **Janitors** — `expire_inactive_schemas` and `expire_stale_thread_jobs` scan with indexed-state
  filters and per-row work proportional to actual stale rows; `_procrastinate_job_status` is a PK
  lookup (`tasks.py:516-553,693-725,819-838`).
- **Jobs endpoint bulk-fetches run progress** (`jobs_views.py:137-144`) rather than per-job.
- **`/health/`** does no DB work (`apps/workspaces/views.py:8-13`), so the 5s frontend ping is
  cheap.
- **MCP `run_materialization` is fire-and-ack** — no server-side sleep/poll loop holding
  connections (no `sleep`/`while` polling in `mcp_server/server.py`).
- **Stream translation** (`apps/chat/stream.py`) is O(events), truncates tool output display at
  2,000 chars, and dedupes tool events by run_id. (Caveat: the 2,000-char truncation is
  display-only; the full content still lives in history/checkpoints — see PC-2.)
- **System prompt cache eviction** — bounded in practice: expired entries are purged when the
  dict exceeds 256 (`base.py:777-783`); the 60s TTL bounds the working set.

## Cross-cutting observation

PC-1 + PC-2 + PC-3 are one defect viewed from three angles: the conversation representation has
no lifecycle. Nothing bounds it in the prompt (cost), in the API call (cache), or at rest
(checkpoints). Any fix should be designed once: a history-compaction policy (window + tool-result
elision + checkpoint pruning) plus `cache_control` on the static prefix would address the top
three findings together. PC-5 (one shared async pool in `mcp_server/services/query.py`) is the
single highest-leverage latency fix and also shrinks PC-6, PC-9, and PC-12.

## Coverage log

**Deep-read (line-by-line):**
`apps/agents/graph/base.py`, `apps/chat/views.py`, `apps/chat/stream.py`, `apps/chat/helpers.py`,
`apps/chat/thread_views.py`, `apps/chat/checkpointer.py`, `apps/chat/urls.py`,
`apps/agents/mcp_client.py`, `apps/agents/memory/checkpointer.py`,
`mcp_server/context.py`, `mcp_server/services/query.py`, `mcp_server/services/metadata.py`,
`apps/knowledge/services/retriever.py`, `apps/workspaces/api/jobs_views.py`,
`apps/workspaces/api/views.py`, `apps/workspaces/api/workspace_views.py` (first 220 lines),
`apps/workspaces/tasks.py` (lines 180–920: materialize, janitors, reconcile, resume helpers),
`apps/artifacts/views.py` (query-data view + outline),
`frontend/src/hooks/useWorkspaceJobs.ts`, `frontend/src/contexts/WorkspaceJobsContext.tsx`.

**Skimmed (targeted greps / partial reads):**
`mcp_server/services/materializer.py` (progress-write sites only), `mcp_server/server.py`
(loop/sleep grep only), `apps/artifacts/models.py` (versioning), `apps/recipes/services/runner.py`
(graph-build sites), `apps/workspaces/services/workspace_service.py` (touch helper),
`apps/workspaces/models.py` (touch methods), `config/deploy.yml` / `config/deploy-worker.yml` /
`Procfile.dev` / `docker-compose.yml` (commands only), `config/settings/base.py` (model/TTL
settings), `pyproject.toml` (dependency floors), `frontend/src/contexts/NetworkStatusContext.tsx`
(interval only), `frontend/src/store/dictionarySlice.ts` (fetch sites only).

**Not examined (honest gaps for the gap loop):**
- `mcp_server/loaders/` (all 19) and the bulk of `materializer.py` — loader-side performance
  (page sizes, per-row INSERT vs COPY, API pagination efficiency, memory buffering of pages)
  is unassessed.
- `mcp_server/services/sql_validator.py` — sqlglot parse cost per query not measured.
- `apps/transformations/` + dbt runner — dbt invocation cost, `threading.Lock` serialization
  effects in `dbt_runner.py` not assessed.
- `apps/users/` (auth, tenant resolution, merge) — login-path query counts not assessed.
- `apps/recipes/services/runner.py` beyond graph-build sites; recipe-run token behavior.
- `apps/agents/tools/*` (artifact/learning/recipe tool bodies), `apps/agents/tracing.py`
  (Langfuse overhead per event), `apps/chat/message_converter.py`, `apps/chat/rate_limiting.py`.
- Most of `frontend/src/` — only the polling hooks/contexts and dictionary slice fetch sites;
  render performance, bundle size, refetch storms on workspace switch unexamined.
- `apps/artifacts/services/export.py` (PDF/print path cost), sandbox HTML generation
  (`artifacts/views.py:34-672`).
- Database indexes/migrations — I did not verify index coverage for the hot filters
  (ThreadJob state, TenantSchema state+last_accessed_at, MaterializationRun lookups).
- Live measurement of any kind (no profiling, no EXPLAIN, no token accounting against the
  Anthropic API) — all findings are static-analysis based.
- `apps/workspaces/services/schema_manager.py`, `workspace_resolver.py` internals
  (`aresolve_workspace` query count assumed small, not traced).
