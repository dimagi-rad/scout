# Seam review: chat ↔ MCP ↔ worker

*Reviewer: seam:chat-mcp-worker. Mandate: own the contract, not the components — tool
schemas, ThreadJob creation vs dispatch atomicity, the resume protocol, who writes which
state when. Report only; no code changed.*

Date: 2026-06-12. HEAD: `35e4230`.

---

## 1. The contract, as actually built

Five parties share this seam:

1. **Chat view** (`apps/chat/views.py:chat_view`) — validates thread ownership, builds the
   agent graph, streams. Writes: `Thread` (upsert), touches schema TTLs.
2. **Agent graph** (`apps/agents/graph/base.py`) — binds MCP tools to the LLM with
   context params *hidden* from the schema (`_llm_tool_schemas`), then *injects*
   `workspace_id`, `user_id`, `thread_id`, `tool_call_id` into every MCP tool call
   (`_make_injecting_tool_node`, base.py:439-477). Writes: nothing directly (LangGraph
   checkpoints via checkpointer).
3. **MCP server** (`mcp_server/server.py`) — 11 tools. Not actually a separate codebase: it
   imports the Django ORM, the Django models, *and the worker's task object*
   (`from apps.workspaces.tasks import materialize_workspace`, server.py:47). Writes:
   `ThreadJob` (create, PENDING), `MaterializationRun` (cancel tool -> FAILED),
   `TenantSchema`/`WorkspaceViewSchema` (teardown tool).
4. **Worker** (`apps/workspaces/tasks.py`) — `materialize_workspace` (chains
   `resume_thread_after_materialization` via a `finally`), two janitors. Writes:
   `ThreadJob` (claim CAS, terminal CAS, janitor flips), `MaterializationRun` (via
   materializer + janitor), checkpointer (resume `ainvoke`, synthetic failure messages).
5. **API job endpoints** (`apps/workspaces/api/jobs_views.py`, `jobs_cancel.py`,
   `materialization_views.py`) — poll/cancel/retry. Writes: `ThreadJob` (CANCELLED),
   `MaterializationRun` (CANCELLED), and — backstop — the same reconcile function the
   worker janitor uses.

### ThreadJob writer map (who writes which state when)

| Transition | Writer | Guard |
|---|---|---|
| (none) -> PENDING | `mcp_server/server.py:623` (run_materialization), `materialization_views.py:198` (retry) | created *after* `defer_async` (see F4) |
| PENDING/CANCELLED -> RUNNING | `tasks.py:1044` (resume claim) | CAS on `state__in=CLAIMABLE_STATES` |
| RUNNING -> terminal | `tasks.py:1264` (resume) | CAS on `state=RUNNING` (preserves concurrent cancel) |
| ACTIVE -> CANCELLED | `jobs_cancel.py:38` | CAS on `state__in=ACTIVE_STATES` |
| RUNNING/PENDING -> FAILED | `tasks.py:754,782,807` (reconcile, from worker janitor *and* API poll) | CAS per branch; `None` procrastinate status = "don't touch this tick" |

This table is the healthiest part of the seam: every transition is a CAS, both cancel
entry points funnel through one function, and reconcile runs from two processes with
identical semantics. The 19-commit fix chain of May 2026 produced something that holds up
under trace.

### The resume protocol

1. MCP `run_materialization`: authz check -> thread-ownership check (defense in depth) ->
   per-thread in-flight dedupe -> `defer_async` -> create ThreadJob(PENDING) -> ack
   `{status: started, thread_job_id}` (server.py:520-648).
2. Worker `materialize_workspace`: per-tenant pipeline loop -> view-schema rebuild ->
   sibling rebuilds -> `finally:` `_defer_resume_for_job` (hedged 0-3.75 s wait for the
   ThreadJob row; janitor backstop if it never appears) (tasks.py:203-396).
3. `resume_thread_after_materialization`: claim CAS -> `_aggregate_materialization_state`
   from `MaterializationRun` rows (source of truth, not the ThreadJob snapshot) ->
   compose a `SYSTEM_RESUME_MARKER` HumanMessage -> bounded `ainvoke` -> terminal CAS
   (tasks.py:1019-1289).
4. Frontend reads assistant output from the checkpointer
   (`thread_views._load_thread_messages`); `message_converter.py:13-20` hides any message
   starting with the marker. Marker producers and the single consumer agree.

---

## 2. Findings

### F1 — Recipe runs are broken: runner calls `build_agent_graph` with a signature that no longer exists — BROKEN-NOW / correctness / verified-by-trace

The recipe runner is a second consumer of the agent-graph contract that was never
migrated when the graph moved from tenant-scoped to workspace-scoped state.

Chain (entry point -> consequence):

1. Frontend `frontend/src/store/recipeSlice.ts:135` —
   `api.post('/api/workspaces/<id>/recipes/<recipeId>/run/')` (wired from
   `RecipesPage.tsx:137 runRecipe`).
2. `config/urls.py:63` -> `apps/recipes/urls.py:22` -> `RecipeRunView.post`
   (`apps/recipes/api/views.py:107-108`): `RecipeRunner(recipe=..., variable_values=...,
   user=request.user)` then `runner.execute()` — **no `graph` argument**, so
   `_provided_graph is None`.
3. `apps/recipes/services/runner.py:189-191`: creates the `RecipeRun` row
   (status=RUNNING), then `graph = async_to_sync(self._build_graph)()` — *outside* the
   try block that starts at line 213.
4. `runner.py:115-119`:
   `build_agent_graph(tenant_membership=self._tenant_membership, user=self.user,
   checkpointer=None)`.
5. `apps/agents/graph/base.py:480-486`:
   `async def build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None,
   oauth_tokens=None)` — there is **no `tenant_membership` parameter and `workspace` is
   required** -> `TypeError` on every call.
6. The exception propagates to `views.py:109-111` -> HTTP 500, and the `RecipeRun` created
   in step 3 is never updated -> orphaned rows stuck in `RUNNING` forever.

Even if the kwarg were fixed, the runner's `initial_state` (`runner.py:215-224`,
`302-311`) still populates the *pre-workspace* AgentState fields (`tenant_id`,
`tenant_name`, `tenant_membership_id`) and omits `workspace_id`/`thread_id`, so the
injection node (`base.py:461`, `state.get(v, "")`) would inject empty `workspace_id`
into every MCP call -> `VALIDATION_ERROR` envelopes; and the recipe `thread_id`
(`recipe-run-<uuid>`) has no `Thread` row, so `run_materialization` would refuse it.
The runner also passes `mcp_tools=None` -> a "data agent" with no data tools.

Why tests didn't catch it: `tests/test_recipes.py:584-696` patches
`apps.recipes.services.runner.build_agent_graph` with a Mock (accepts any kwargs) or
passes `graph=mock_graph`. The seam is exactly what the mocks erase.

Reachable via: Recipes page "run" -> `POST /api/workspaces/<id>/recipes/<id>/run/`.
Note the agent's `save_as_recipe` tool actively creates recipes users then cannot run.
Complexity: accidental (rename residue from the workspace migration).

### F2 — MCP `cancel_materialization` writes FAILED, not CANCELLED: it does not cancel anything under the current worker contract — LATENT / correctness / verified-by-trace

The cancellation contract has exactly one trigger the worker honors:
`MaterializationRun.state == CANCELLED`, checked by the progress updater between pages
(`tasks.py:485-494`) and by the materializer's CAS transitions
(`materializer.py:236-244, 430-443, 461-477`). The canonical canceller respects this:
`jobs_cancel.py:30-41` flips runs to `CANCELLED` *then* aborts the procrastinate job.

The MCP tool does not (`server.py:445-493`):

- `server.py:479` `run.state = MaterializationRun.RunState.FAILED` — the updater never
  raises (`FAILED != CANCELLED`), so the LOAD phase keeps running to the end of the
  phase; the worker keeps loading data for a run the tool reported `"cancelled": true`.
- It does not abort the procrastinate job and does not touch the ThreadJob, so the
  spinner/resume lifecycle proceeds as if nothing happened; the eventual CAS miss at
  `materializer.py:435-443` converts it into a quasi-cancel only after the entire LOAD
  phase completes.
- Its sibling `get_materialization_status` (`server.py:408-415`) carries a docstring
  claiming "live progress is delivered via MCP progress notifications during an active
  run_materialization call" — that protocol no longer exists; `run_materialization` is
  fire-and-ack and `server.py` contains zero `report_progress` calls.

Reachability is the saving grace and a finding in itself: both tools take `run_id`, and
under the fire-and-ack flow **the agent never learns a run_id** — `run_materialization`
returns `thread_job_id` (server.py:637-648) and the resume summary
(`_aggregate_materialization_state`, tasks.py:928-1016) contains tenant/sources but no
run ids. So the LLM is bound to two tools (they are in `mcp_tools`, all of which
`_build_tools` attaches, base.py:692) that it can only invoke with hallucinated UUIDs.
Three cancel implementations now exist (`jobs_cancel.py`, `materialization_views.py`
orphan path, MCP tool), and one of them disagrees with the worker's contract.

Complexity: accidental — residue of the pre-ThreadJob, in-call polling era.

### F3 — `SchemaState.MATERIALIZING` is a phantom state: never written, but load-bearing readers — and the single-tenant "in progress" guard rests on it — LATENT / correctness / verified-by-trace (consequence: strong-inference)

No non-test code writes `MATERIALIZING` (repo-wide grep: only `models.py:18` defines it
and ~10 sites *read* it; `git log -S` shows the writers lived in the Celery-era refresh
path and did not survive). Consequences across the seam:

- `base.py:230-237`: the single-tenant prompt branch "A materialization is already in
  progress... Do NOT trigger another one" can never fire. The multi-tenant variant
  checks `MaterializationRun.ACTIVE_STATES` (`base.py:318-332`); the single-tenant
  variant checks only the phantom schema state (`base.py:211-214`) — an asymmetry, so a
  single-tenant workspace's prompt has **no in-progress signal at all**: during a
  background run `provision()` has already set the schema ACTIVE
  (`schema_manager.py:120-122`), so the prompt says "Data is loaded and ready" with
  whatever tables the last run left.
- `server.py:692-695, 738-741` (`get_schema_status`) and the touch paths
  (`workspace_service.py:92-110`) filter on a state that cannot occur.

Combined with the deliberately thread-scoped dedupe in `run_materialization`
(`server.py:576-604` — the comment itself concedes "this lets two threads in the same
workspace dispatch parallel materializations that share tenant_schemas... the
materializer has no advisory lock per tenant_schema"), the system has no layer left that
prevents two concurrent `run_pipeline` executions against the same physical tenant
schema: not the prompt (dead guard), not the dispatch guard (thread-scoped), not the
materializer (no lock). Two concurrent loads drop/recreate the same `raw_*` tables;
result is torn or duplicated data until the next clean run. Reachable today: two chat
threads (or two users) in one workspace, or two workspaces sharing a tenant.

Complexity: accidental (state-machine vestige + guard erosion across refactors).

### F4 — ThreadJob created after dispatch: the acknowledged ordering race, mitigated but still the contract's soft spot — DEBT / correctness / verified-by-trace

`server.py:606-635`: `defer_async` first, `ThreadJob.acreate` second, no shared
transaction (the in-code "Atomicity note" admits it; rollback is best-effort
`cancel_job_by_id_async(abort=True)`). The worker hedges: `_defer_resume_for_job`
(tasks.py:363-396) polls for the row over ~3.75 s, then leaves it to the janitor
(<=10 min staleness + */15 cron => up to ~25 min of phantom spinner in the worst case).
`tasks.py:373-376` TODO documents the proper fix (placeholder ThreadJob before dispatch,
nullable `procrastinate_job_id`). The retry endpoint duplicates the same
dispatch-then-create pattern (`materialization_views.py:184-210`), so the fix must land
in two places. Nothing here is unknown to the team; it is recorded because the seam's
correctness depends on the hedge constants and on the janitor staying alive (the
2026-06-09 incident was precisely the janitor dying).

Complexity: accidental; the TODO's design is the cure.

### F5 — Resume prompt tells the agent to "continue with the user's original request using the now-loaded data" for FAILED and CANCELLED materializations — DEBT / correctness / verified-by-trace (text), strong-inference (LLM impact)

`tasks.py:1109-1125`: the body selection handles `view_schema_failed`, `no_runs`,
`partial` — and then one `else` covers **completed, cancelled, and failed** alike:

> "Materialization just completed (status={status}). Please continue with the user's
> original request using the now-loaded data."

For `status="failed"` (reachable: any tenant's pipeline raises — e.g. Connect 5xx
exhaustion -> `_aggregate_materialization_state` returns "failed", tasks.py:1002-1005)
the ThreadJob is correctly flipped FAILED with an `error_summary`
(tasks.py:1236-1257), but the agent is simultaneously instructed that data is loaded.
Same for genuine mid-load cancels. The per-tenant `summary` embedded in the message is
the only thing preventing the agent from confidently narrating success — the same
failure mode as the 2026-06-10 incident (d), which was fixed for the view-schema case
only. The fix is one or two additional branches mirroring the `partial` wording.

Complexity: accidental.

### F6 — The context-injection contract works only because both ends skip validation; the prompt still instructs a `pipeline=` argument the tool dropped — DEBT / velocity (+ COSMETIC drift) / verified-by-trace

The injecting node adds `workspace_id`, `user_id`, `thread_id`, `tool_call_id` to
*every* tool call in `MCP_TOOL_NAMES` (base.py:456-472) — including tools like
`list_tables(workspace_id)` and `query(sql, workspace_id)` that declare none of the
last three. This survives because of two independent library behaviors, neither of
which Scout pins or tests:

- LangChain `BaseTool._parse_input` with a dict `args_schema` (what
  `langchain_mcp_adapters` produces) **returns the input unvalidated**
  (verified in the installed `langchain_core`: `if isinstance(input_args, dict):
  return tool_input`).
- FastMCP validates against `ArgModelBase` whose `model_config =
  ConfigDict(arbitrary_types_allowed=True)` — pydantic-v2 default `extra="ignore"`, so
  undeclared args are silently dropped (verified in the installed `mcp` package).

If either library tightens (jsonschema validation client-side, `extra="forbid"`
server-side), **every MCP tool call fails at once**. The same silent-drop behavior
masks a live prompt<->schema drift: `base.py:221-228` still instructs the agent to
'Call `run_materialization` with `pipeline="..."`', but the tool has had no `pipeline`
parameter since fire-and-ack (`server.py:521-527`); the argument is swallowed and the
worker picks the pipeline by provider (`tasks.py:248-253`). Harmless today; exactly the
prompt-drift class that produced #190/93504d5.

Complexity: accidental (an implicit dependency that should be an explicit test or an
allow-listed pass-through).

### F7 — MCP authz is "trust the network": every tool, including destructive teardown, is keyed by workspace_id alone — LATENT / security / verified-by-trace (mechanism)

All tenant-scoped tools resolve context purely from the `workspace_id` argument
(`server.py:71-75`, `context.py:83-139`). `run_materialization` is the only tool with a
membership check (server.py:553-570). `teardown_schema(confirm=True, workspace_id=...)`
(server.py:801-865) drops every tenant schema and the view schema for any workspace,
with no caller identity at all. The protections are: internal-only networking +
DNS-rebinding allowlist (`server.py:904-912`) and the agent graph being the intended
sole caller. Any SSRF/pivot that can POST to port 8100 owns every workspace's data.
This matches TODO.md's unchecked security items (per-tenant role isolation exists for
*queries* via `SET ROLE` in `services/query.py`, but tool-level caller auth does not).
Likely replicated by the security lens; recorded here because it *is* the chat<->MCP
trust contract.

Complexity: mixed — "trust the private network" is a defensible essential choice, but
the asymmetry (one tool checks membership, the destructive one checks nothing) is
accidental.

### F8 — Workspace-level cancel can cancel the same user's run in a *different* workspace (shared tenants) — LATENT / correctness / verified-by-trace

`materialization_views.py:46-70`: cancel selects active runs by
`tenant_schema__tenant__in=workspace.tenants.all()` — but tenant schemas are shared
across workspaces — and then matches ThreadJobs by `thread__user=user` **without a
workspace filter**. If the same user has a chat-driven materialization running in
workspace B that shares a tenant with workspace A, cancelling from A cancels B's run
and injects a "cancelled" resume into B's thread. The orphan-run path (lines 93-117)
similarly cancels `/refresh/`-spawned runs belonging to sibling workspaces. Small blast
radius (same user, or untracked runs), but it violates the "cancel acts on this
workspace" contract the endpoint name promises.

### F9 — No serialization between a resume `ainvoke` and a live user turn on the same thread — LATENT / correctness / strong-inference

`chat_view` does not check for an active/RUNNING ThreadJob before invoking the graph;
`resume_thread_after_materialization` does not check whether a user turn is streaming.
Both run `ainvoke`/`astream_events` against the same LangGraph `thread_id` checkpoint
concurrently (chat: `views.py:237-249`; resume: `tasks.py:1161-1164`). The
dangling-tool-call repairs (`helpers.repair_dangling_tool_calls`, plus the in-graph
guard `base.py:551-578`) fix the *Anthropic protocol* symptom, but interleaved
checkpoint writes from two writers can still interleave messages or strand one writer's
superstep. The system prompt nudges the agent to "end your turn" during
materialization, but nothing enforces it at the seam. No incident attributed yet;
flagged because every other multi-writer surface on this seam eventually grew a CAS.

### F10 — `prune_messages` is dead code; conversation history is unbounded — DEBT / cost-perf / verified-by-trace

`apps/agents/graph/state.py:24` has zero non-test callers (repo grep). `AgentState`
uses the `add_messages` reducer with no pruning hook, so every turn replays the entire
thread history (plus a system prompt that already embeds full schema context,
base.py:699-785) to the model. Cost grows linearly per thread with no cap; the module
docstring still advertises the pruning strategy as if active (comments-vs-code
mismatch).

### F11 — Residual shape-drift crumbs on the status path — COSMETIC / correctness / verified-by-trace

- `server.py:719-724` (`get_schema_status`): falls back to a legacy
  `{"table", "rows_loaded"}` result shape that no current writer produces — a third
  spelling of "what tables exist" on this seam alone.
- `stream.py:190-198`: live SSE emits `tool-input-available` with `"input": {}` always
  and only at `on_tool_end`, while the checkpointer-replay path
  (`message_converter.py:56-71`) emits real `args`. Same transcript, two different
  shapes depending on whether you watched live or reloaded.
- `mcp_client.py` circuit breaker is process-global module state across all users of
  the API process — fine today, noted as a shared-state crumb.

---

## 3. What's fine (verified healthy)

- **ThreadJob state machine**: claim CAS excludes RUNNING (duplicate-ainvoke
  prevention), terminal CAS scoped to RUNNING (preserves concurrent cancel and re-reads
  actual state for the return value) — traced end to end, sound (tasks.py:1043-1289).
- **Cancel ordering contract**: DB flip before procrastinate abort, single funnel
  (`cancel_thread_job`) used by both cancel endpoints; matches the worker's
  page-boundary checkpoint exactly (jobs_cancel.py docstring is accurate to the code).
- **Janitor + API backstop**: `_procrastinate_job_status` returns None-means-don't-touch
  (no misclassification on DB blips); identical reconcile logic runs in both worker and
  API processes — direct, correct response to the 2026-06-09 incident.
- **Materializer state CASes**: every DISCOVERING->LOADING->TRANSFORMING->COMPLETED
  transition is conditional, so an external CANCELLED is never overwritten; pre-loop
  failures get a terminal FAILED stamp (materializer.py:236-495).
- **Thread ownership**: chat view rejects cross-user/cross-workspace thread attachment
  with 404 (views.py:117-137), and MCP re-checks before binding a ThreadJob
  (server.py:560-570) — defense in depth that actually exists.
- **provision() resurrect path** resets `last_accessed_at` (schema_manager.py:114-122) —
  the 2026-06-10 TTL fix is present and commented with the right reason.
- **`_defer_resume_for_job` in `finally`** covers early-return paths, so "workspace
  missing"/"no memberships" no longer strand spinners (tasks.py:356-360).
- **Connection-hygiene task decorator** (config/procrastinate.py) wraps every task and is
  enforced by `tests/test_worker_db_resilience.py`; clearly marked temporary with the
  upstream issue linked.
- **View-schema failure surfacing** (the #229/#230 fixes): resume inspects the
  `WorkspaceViewSchema` row directly and `get_schema_status` reports `state: failed`
  with `last_error` — both sides of that sub-contract agree.
- **Resume marker contract**: all four producers in tasks.py use `SYSTEM_RESUME_MARKER`;
  the single consumer (`message_converter.py`) filters on it.

## 4. Contract-drift pattern

Four of the findings (F1, F2, F3, F6) are the same shape: the chat<->MCP<->worker spine
moved (tenant->workspace state, in-call polling->fire-and-ack ThreadJob, Celery
refresh->procrastinate materialize), and a secondary consumer of the old contract was
left behind — the recipe runner, the run_id-keyed MCP tools, the MATERIALIZING readers,
the `pipeline=` prompt line. The spine itself is well-tended (it gets the fix commits);
the periphery that *calls* the spine does not. A cheap structural counter: any test that
invokes the real `build_agent_graph` signature from the recipe path, and a schema-level
test asserting the prompt's tool instructions match the live MCP tool schemas.

## 5. Coverage log

**Deep-read (line by line):**
`apps/chat/views.py`, `apps/chat/models.py`, `apps/chat/stream.py`, `apps/chat/helpers.py`,
`apps/chat/message_converter.py`, `apps/chat/checkpointer.py`, `mcp_server/server.py`,
`mcp_server/context.py`, `mcp_server/envelope.py`, `mcp_server/__main__.py`,
`apps/workspaces/tasks.py`, `apps/agents/graph/base.py`, `apps/agents/graph/state.py`,
`apps/agents/mcp_client.py`, `apps/workspaces/api/jobs_views.py`,
`apps/workspaces/api/jobs_cancel.py`, `apps/workspaces/api/materialization_views.py`,
`config/procrastinate.py`, `apps/recipes/services/runner.py`,
`mcp_server/services/materializer.py` lines 96-510 (run_pipeline state machine),
`apps/workspaces/services/schema_manager.py` lines 55-145 (provision),
installed-library verification: `langchain_core.tools.base.BaseTool._parse_input`,
`mcp.server.fastmcp.utilities.func_metadata.ArgModelBase`.

**Skimmed:** `apps/chat/thread_views.py` (outline + first 60 lines),
`apps/workspaces/services/workspace_service.py` (touch helpers),
`apps/recipes/api/views.py` (first 120 lines), `apps/recipes/urls.py`, `config/urls.py`
(workspace_urlpatterns region), `apps/agents/prompts/base_system.py` (grep only),
`mcp_server/services/materializer.py` function inventory (grep),
`frontend/src/store/recipeSlice.ts` + `RecipesPage.tsx` (run wiring only),
`tests/test_recipes.py` (mock-usage grep only), `apps/workspaces/models.py` (grep).

**Not examined:** frontend job/chat plumbing (`useWorkspaceJobs`, `ChatPanel`,
`useWorkspaceThreadSync`, `MaterializationProgressBanner`) — the frontend half of the
poll contract is asserted from backend shapes only; `mcp_server/loaders/*` and the
per-table writer functions (materializer.py 510-1972); `mcp_server/services/query.py`,
`sql_validator.py`, `metadata.py`, `dbt_runner.py`; `apps/agents/memory/checkpointer.py`
and LangGraph checkpoint internals (concurrency claim F9 is inference, not a
checkpoint-level trace); `apps/chat/rate_limiting.py`; the legacy `/refresh/` views in
`apps/workspaces/api/views.py`; `apps/agents/tools/*` (artifact/learning/recipe tools);
`apps/agents/tracing.py`; procrastinate library abort/cancellation semantics beyond what
the code asserts; all of `tests/` except the recipe-mock grep; knowledge retriever;
allauth token refresh; deploy configs.
