# Vertical review: Materialization & schema lifecycle

*Reviewer: vertical-materialization · 2026-06-12 · HEAD 35e4230*

Scope: pipeline registry, loaders/writers, TenantSchema / MaterializationRun /
ThreadJob / WorkspaceViewSchema state machines, TTL janitors, legacy refresh path,
cancellation, resume-after-materialization. Every entry point traced end to end:
MCP tools (`run_materialization`, `cancel_materialization`, `get_materialization_status`,
`get_schema_status`, `teardown_schema`, `list_pipelines`), HTTP views (`/refresh/`,
`/materialization/cancel/`, `/materialize/retry/`, `/jobs/active/`, `/jobs/<id>/cancel/`),
cron (`expire_inactive_schemas` */30, `expire_stale_thread_jobs` */15), and the
worker tasks they drive.

---

## Capability scorecard (what % actually works)

| Capability | Est. functional | Notes |
|---|---|---|
| Chat-driven materialization (MCP `run_materialization` → worker → resume) | ~90% | Demo path solid after the May fix chain; residual races below (F2 undermines it for runs >10 min) |
| Resume-after-materialization | ~85% | CAS chain is genuinely robust; F2 (janitor flips healthy RUNNING resumes) is the remaining hole |
| Multi-tenant view-schema lifecycle | ~70% | Full-success path correct incl. sibling rebuilds (#227–#230); partial-failure and cancel paths leave a lying ACTIVE view schema (F6); TTL-touch sibling miss (F7) |
| TTL janitors | ~80% | Incident-b fix correct for `provision()`; TEARDOWN-resurrect race (F3) and view-schema touch miss (F7) are surviving members of the same class |
| Cancellation | HTTP ~95% / MCP-tool ~30% | HTTP path ordering is correct; MCP `cancel_materialization` is contract-drifted and unscoped (F5) |
| Legacy `/refresh/` path | 0% (actively destructive) | Loads into the live schema then destroys it; still wired to the Data Dictionary UI (F1) |
| MCP `teardown_schema` tool | ~40% | Drops physical schemas but leaves every state row lying; can poison resume cursors into silent data loss (F4) |
| Catalog truth (`pipeline_list_tables`) | ~95% | #185 reconciliation verified present and correct; `get_schema_status` table shape is dead (F10) |
| Loaders (Connect / OCS / CommCare) | Connect ~90%, OCS/CommCare ~70% | Retry, error-shape and resume hardening applied only to Connect (F12, F14) |

---

## Findings

### F1 — BROKEN-NOW / data-loss: `/refresh/` loads data into the live schema, then destroys it

The v1-run-A S1 is still fully present and still wired to the UI.

Chain (verified by trace):
1. UI: `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx:36` → `refreshSchema()` → `frontend/src/store/dictionarySlice.ts:197` `api.post('/api/workspaces/<id>/refresh/')`.
2. `apps/workspaces/api/views.py:362` `RefreshSchemaView.post` → `SchemaManager().create_refresh_schema(tenant)` → `apps/workspaces/services/schema_manager.py:176` creates a **new** TenantSchema named `{base}_r{8hex}` (PROVISIONING) → defers `refresh_tenant_schema`.
3. `apps/workspaces/tasks.py:150` creates the physical `_r` schema; `tasks.py:173` calls `run_pipeline(membership, credential, pipeline_config)` — **without passing the new schema**.
4. `mcp_server/services/materializer.py:183` — `run_pipeline` calls `SchemaManager().provision(tenant_membership.tenant)`, which resolves by the **base** sanitized name (`schema_manager.py:66`) and returns the *existing ACTIVE* schema. All data is loaded there. The `_r` schema receives nothing.
5. `tasks.py:182-184` marks the empty `_r` schema ACTIVE; `tasks.py:188-197` flips the base schema (which just received the fresh data) to TEARDOWN and schedules `teardown_schema` in 30 min → `DROP SCHEMA ... CASCADE`, runs flipped STALE (`tasks.py:639-645`).

Consequence: immediately after "refresh", routing (`mcp_server/context.py:56`, ACTIVE-only filter) points at the empty `_r` schema — catalog and queries go empty; 30 minutes later the only materialized copy of the data is physically dropped. Recovery requires a fresh full materialization.

Why tests don't see it: `tests/test_refresh_task.py` patches `apps.workspaces.tasks.run_pipeline` outright (line 73), so the wrong-target load is invisible (see F16).

- Status: BROKEN-NOW · Impact: data-loss · Confidence: verified-by-trace · Complexity: accidental (contract drift between `refresh_tenant_schema`'s schema-handoff design and `run_pipeline`'s self-provisioning)
- Reachable via: Data Dictionary page refresh button (`handleRefresh`), `POST /api/workspaces/<id>/refresh/`.

### F2 — BROKEN-NOW / correctness: stale-job reconciler kills healthy in-flight resumes for materializations >10 min

`reconcile_stale_thread_job` measures staleness from `ThreadJob.created_at`, but a ThreadJob legitimately enters RUNNING only when the *resume* task claims it — which for a long materialization happens long after creation. The reconciler checks the procrastinate status of `tj.procrastinate_job_id`, which is the **materialize** job, not the resume job, so a healthy in-flight resume is indistinguishable from a crashed one.

Chain (verified by trace):
1. Materialization takes >10 min (large Connect opportunity / OCS messages N+1 — entirely normal). `STALE_JOB_THRESHOLD = timedelta(minutes=10)` (`apps/workspaces/tasks.py:690`), pinned by `tests/test_threadjob_janitor.py:270`.
2. Worker finishes `materialize_workspace`; procrastinate status of the materialize job flips to `succeeded`; the chained resume claims the ThreadJob PENDING→RUNNING (`tasks.py:1044-1047`) and enters `agent.ainvoke` (up to `AGENT_RESUME_TIMEOUT_S` = 120 s).
3. Frontend polls `GET /jobs/active/` every 3 s (`frontend/src/hooks/useWorkspaceJobs.ts:4`). `apps/workspaces/api/jobs_views.py:123-129` reconciles every active ThreadJob with `created_at < now-10min` — true for this job since minute 10.
4. `tasks.py:742-748`: status is `succeeded` (not `todo`/`doing`) → falls into `tasks.py:750-774`: `tj.state == RUNNING` → CAS flips RUNNING→FAILED with the "interrupted (likely a server restart). Please re-ask" summary, and `_persist_synthetic_failure_message` injects a synthetic AIMessage into the **same LangGraph thread the resume is concurrently writing to**.
5. The real resume finishes; its terminal CAS (`tasks.py:1264-1271`, filtered on `state=RUNNING`) matches zero rows and logs "not clobbering".

Consequence: for any materialization whose end-to-end time exceeds 10 minutes with the workspace tab open (3 s poll makes the race essentially deterministic; the */15 worker janitor gives a ~10% chance even with the tab closed), the user gets a FAILED job card and a spurious "interrupted, please re-ask" message racing the real answer, and the ThreadJob terminal state is wrong. The synthetic `aupdate_state` racing the in-flight `ainvoke` checkpoint writes is an additional unstudied hazard.

- Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace (code path; not reproduced live) · Complexity: accidental — staleness epoch should be the RUNNING transition (no `started_at` field exists) or the reconciler should check the *resume* job's status.
- Reachable via: `useWorkspaceJobs` poll + `expire_stale_thread_jobs` cron, on every materialization >10 min.

### F3 — LATENT / data-loss: `provision()` resurrects TEARDOWN rows while a queued `teardown_schema` task will still drop them

`SchemaManager.provision` (`schema_manager.py:80-93`): for a row in TEARDOWN state, `create()` hits IntegrityError, the refetched row is not ACTIVE/MATERIALIZING, and the code falls through to `CREATE SCHEMA IF NOT EXISTS` + sets ACTIVE with fresh `last_accessed_at` (`schema_manager.py:120-122`). But the teardown task already queued for that row (`tasks.py:544`, or the 30-min delayed one at `tasks.py:195-197`) performs the physical DROP **without re-checking state** (`tasks.py:609-664` — fetches the row and calls `manager.teardown` unconditionally).

Sequence: TTL janitor flips a stale-but-ACTIVE schema to TEARDOWN and defers teardown → user-triggered materialization (the classic "user returns after idle" moment) runs `provision()` first → row resurrected ACTIVE, pipeline loads 20 min of data → queued teardown executes → `DROP SCHEMA ... CASCADE` on the freshly loaded schema, runs flipped STALE, row EXPIRED. The 30-minute delayed teardown scheduled by the refresh path (F1) arms the same bomb with a much larger window.

This is the surviving sibling of the 2026-06-10 incident-b fix: the fix reset the TTL on resurrect but did not make teardown state-aware (e.g. `if schema.state != TEARDOWN: return`) or invalidate the queued task on resurrect.

- Status: LATENT (timing window between janitor flip and teardown execution; widened when the single worker is busy running the materialization itself) · Impact: data-loss · Confidence: verified-by-trace for the mechanism; probability assessment is inference · Complexity: accidental.

### F4 — LATENT / data-loss + state-drift: MCP `teardown_schema` tool drops physical schemas without touching any state row

`mcp_server/server.py:801-865`: the tool calls `mgr.ateardown_view_schema(vs)` and `mgr.ateardown(ts)` — both physical-DROP-only (`schema_manager.py:474-512`, docstring: "callers are responsible for updating the model state") — and then **updates nothing**: no `TenantSchema.state`, no `WorkspaceViewSchema.state`, no `MaterializationRun` → STALE, no `_fail_dependent_view_schemas` for sibling workspaces (the exact lie PR #230 fixed for the task path).

Consequences, in increasing severity:
1. State rows lie: `TenantSchema` stays ACTIVE over a nonexistent schema; `get_schema_status` keeps reporting `exists=True, state=active` with a stale `last_materialized_at`. (Catalog itself self-heals — `pipeline_list_tables` reconciles against `information_schema`, `mcp_server/services/metadata.py:76-84`.)
2. Sibling multi-tenant workspaces sharing the tenant have their views cascade-dropped while their `WorkspaceViewSchema` rows stay ACTIVE — `workspace_list_tables` then returns empty over a lying ACTIVE schema.
3. **Resume-cursor poisoning**: because the tool does not flip COMPLETED/PARTIAL runs to STALE (unlike `tasks.py:639-645`), a prior PARTIAL run with a `cursor_state` watermark remains the most recent run. The next materialization reads it (`materializer.py:583-605`), passes `start_cursor`, and the resumable writer **skips the DROP** and `CREATE TABLE IF NOT EXISTS` on the now-empty schema (`materializer.py:1418-1423` etc.), then loads only rows with id > watermark. The source is marked `completed` while all pre-watermark rows are silently missing.

- Status: LATENT (needs the agent to invoke teardown — it is in `MCP_TOOL_NAMES`, `apps/agents/graph/base.py:73`, so it is one `confirm=True` away — followed by a materialization whose prior run was PARTIAL) · Impact: data-loss / correctness · Confidence: verified-by-trace · Complexity: accidental (two teardown implementations, one canonical and one bare).

### F5 — LATENT / correctness + authz: MCP `cancel_materialization` is contract-drifted from the worker's cancellation protocol and unscoped

Three independent defects in one tool (`mcp_server/server.py:445-493`):

1. **Wrong state**: it sets `state=FAILED` (line 479), but the worker's cancellation checkpoint only honors CANCELLED — `apps/workspaces/tasks.py:493` `if current_state == MaterializationRun.RunState.CANCELLED: raise MaterializationCancelled()`. A cancel issued through this tool mid-LOAD therefore does **not** stop the load: every remaining page and every remaining source loads to completion; the run only halts at the next phase-boundary CAS (`materializer.py:435-443`), which then unconditionally overwrites `result` with `{"cancelled": True, ...}` on a FAILED-state row.
2. **Wrong narrative**: `_aggregate_materialization_state` reports `failed`, so the resume tells the user the materialization failed when they asked to cancel it.
3. **No authz scoping**: the tool takes a raw `run_id` with no workspace/user check (compare `run_materialization`'s membership guard, `server.py:555`). It is bound to the LLM with its schema unmodified — it is *not* in `MCP_TOOL_NAMES` (`graph/base.py:65-76`), and `_build_tools` (`graph/base.py:692`) passes **all** MCP tools through, so the agent supplies `run_id` itself and can cancel (or with `get_materialization_status`, inspect) any tenant's run platform-wide given a UUID. The HTTP cancel path got careful cross-user protections (`materialization_views.py:57-93`); this parallel path got none.

The HTTP path (`jobs_cancel.cancel_thread_job`) is correct on all three axes — this is a second, drifted implementation of cancel.

- Status: LATENT (reachable whenever the agent chooses this tool — e.g. user says "cancel" in chat and the LLM picks the MCP tool over ending its turn) · Impact: correctness, minor security · Confidence: verified-by-trace · Complexity: accidental.

### F6 — LATENT / correctness: partial-failure or cancel during multi-tenant rematerialization leaves THIS workspace's view schema cascade-dropped but ACTIVE

`materialize_workspace` rebuilds the workspace's own view schema only when `all_succeeded` (`tasks.py:322-336`). But the per-tenant loads that *did* succeed have already `DROP TABLE ... CASCADE`d their raw tables (every writer; e.g. `materializer.py:1419`), which cascade-drops the namespaced views inside this workspace's own `ws_*` schema. The sibling rebuild explicitly excludes the current workspace (`tasks.py:434`). The resume task's view-schema-failure detection only fires when `vs.state != ACTIVE` (`tasks.py:1078-1083`) — the row is still ACTIVE, so the agent is told "partial — answer what you can", then every query through the view schema hits missing views (including for the tenants that loaded successfully) and the agent walks into NOT_FOUND territory (the panic-loop class #190 guards against).

The cancel path is identical: cancel → break → `all_succeeded=False` → no rebuild.

PRs #227–#230 fixed this exact "ACTIVE-but-empty view schema" lie for *sibling* workspaces and for the *teardown* path; the same-workspace partial/cancel path was missed.

- Status: LATENT · Impact: correctness · Confidence: verified-by-trace (PostgreSQL CASCADE semantics + code path) · Complexity: accidental.

### F7 — LATENT / correctness: `build_view_schema` reactivates EXPIRED view-schema rows without resetting `last_accessed_at` (incident-b class, view-schema edition)

The 2026-06-10 incident-b fix added the TTL reset to `provision()` (`schema_manager.py:114-122`) and bulk-touching to `touch_workspace_schemas`. `build_view_schema` got neither: on success it saves `update_fields=["state", "last_error"]` only (`schema_manager.py:436-438`). A `WorkspaceViewSchema` row resurrected from EXPIRED (via `get_or_create` returning the old row, `schema_manager.py:280-287`) keeps its >24h-old `last_accessed_at`; `expire_inactive_schemas` (`tasks.py:547-553`) will flip it to TEARDOWN on its next tick and drop the freshly built schema. The window is narrowed because `load_workspace_context` touches the row on every MCP call (`context.py:125`) and the resume agent usually queries immediately — but a janitor tick landing between build and first MCP touch (up to 30 min cron granularity) reproduces the incident for view schemas.

Also note the misleading comment in `rebuild_workspace_view_schema` (`tasks.py:573-575`): "build_view_schema already saves state=FAILED before re-raising" — false for the early `ValueError`s raised *before* the row is fetched/created (`schema_manager.py:258-275`), which can leave a row parked in PROVISIONING with no `last_error`.

- Status: LATENT · Impact: correctness · Confidence: strong-inference (all hops quoted; window probabilistic) · Complexity: accidental.

### F8 — DEBT / correctness: no per-tenant mutual exclusion for materialization — acknowledged in a comment, unmitigated

`mcp_server/server.py:580-585` (comment in `run_materialization`): two threads in one workspace, or two workspaces sharing a tenant, can dispatch parallel `materialize_workspace` runs against the **same physical tenant schema**; "the materializer has no advisory lock per tenant_schema." Interleaved `DROP TABLE`/`CREATE`/`INSERT` on shared tables from two workers is undefined: deadlocks at best, interleaved/mixed table contents at worst, with both runs recording COMPLETED. The in-flight guard is thread-scoped only (`server.py:586-590`).

- Status: DEBT (requires worker concurrency > 1 or multiple workers; deploy uses a dedicated worker config — not verified how many slots) · Impact: data-loss potential · Confidence: verified-by-trace for the absence of a lock; consequence is inference · Complexity: accidental.

### F9 — DEBT (acknowledged): ThreadJob created after `defer_async` — fire-and-ack ordering race

`server.py:606-635` defers the job, then creates the ThreadJob; `tasks.py:363-395` hedges with a 3.75 s backoff and hands off to the janitor otherwise; `tasks.py:373` TODO documents the proper fix (pre-create with nullable `procrastinate_job_id`). Failure mode today: under load, a finished materialization whose ThreadJob became visible late gives the user a phantom spinner for up to ~10–25 min (janitor threshold + cron) before the resume fires.

- Status: DEBT · Impact: correctness (UX latency) · Confidence: verified-by-trace (and self-documented) · Complexity: accidental.

### F10 — DEBT / contract drift: `get_schema_status` single-tenant table list reads a result shape no writer has produced since the pipeline rewrite

`server.py:718-724` reads `last_run.result["tables"]` or `["table"]+["rows_loaded"]`; `run_pipeline` persists `{"pipeline", "sources", "transforms"}` (`materializer.py:465-477`). The branch is dead — single-tenant `get_schema_status` always returns `tables: []` even with data fully loaded, while reporting `exists=True, state=active`. Mostly absorbed because the prompt-assembly pre-fetches schema context separately (`graph/base.py:757`), but any consumer trusting this field sees an empty workspace.

- Status: DEBT · Impact: correctness (minor) · Confidence: verified-by-trace · Complexity: accidental (rename residue).

### F11 — DEBT / dead state: `SchemaState.MATERIALIZING` is never written

Grep over apps/ + mcp_server/ shows no writer of `state=MATERIALIZING`; at least four readers branch on it (`context.py:58`, `graph/base.py:230,325`, `workspace_views.py:85`, `api/views.py` filters, `workspace_service.py:92,102`). The pipeline instead leaves schemas ACTIVE while loading (provision sets ACTIVE up front — `schema_manager.py:120`), which also means "data readable" status is reported during a DROP/recreate load window. The dead state misleads readers of the state machine and the UI's "provisioning" derivation.

- Status: DEBT · Impact: velocity / correctness (status truthfulness) · Confidence: verified-by-trace (grep) · Complexity: accidental.

### F12 — DEBT / sibling gap: retry & error-shape hardening applied only to Connect loaders

- `connect_base.py` has bounded urllib3 Retry (4 attempts, backoff, Retry-After) and raises `ConnectExportError` on missing `results`.
- `ocs_base.py` and `commcare_base.py` have **no retry at all**; one transient 502 on page N of an hours-long CommCare forms load (or one of the N+1 OCS per-session message fetches) fails the whole source; OCS/CommCare sources are non-resumable (`materializer.py:257` — resumable path is Connect-only), so the single transaction rolls back and the next run re-fetches everything.
- `ocs_base._paginate` (`ocs_base.py:75`) silently treats a missing `results` key as an empty page — a malformed OCS response yields a source marked `completed` with 0 rows instead of an error (the exact silent-fallback Connect was fixed for).

- Status: DEBT · Impact: cost-perf + correctness · Confidence: verified-by-trace (code); operational frequency is inference · Complexity: accidental.

### F13 — LATENT / input-validation: tenant schema names — sanitization collisions and unguarded 63-byte truncation

`_sanitize_schema_name` (`schema_manager.py:625-631`) lowercases, maps `-`→`_`, strips other punctuation. Distinct external ids can collide (`a-b` vs `a_b` vs `a.b` → `a_b`/`ab`), and `provision()` resolves **by schema_name alone** (`schema_manager.py:68-71`) — a colliding tenant gets *another tenant's* TenantSchema row (FK pointing at the first tenant), and the materializer would overwrite its tables. Separately, the 63-byte identifier guard added for view names after incident-1a was not applied to tenant schema names: a long external_id stores a 255-char `schema_name` in Django while PostgreSQL silently truncates the physical name, and two long ids sharing a 63-byte prefix collapse to one physical schema. Current providers make both unlikely (Connect: integer ids; OCS: opaque ids; CommCare domains are short and hyphen-only), which is why this is latent, but it is the recurring identifier-shape class.

- Status: LATENT · Impact: data-loss (cross-tenant overwrite) if triggered · Confidence: strong-inference (collision mechanics verified; provider id shapes not exhaustively verified) · Complexity: accidental.

### F14 — LATENT / data integrity: resume watermark depends on a payload field (`id`) that is never persisted

Resumable Connect writers advance the cursor via `_max_id(page, "id")` (`materializer.py:1237-1247`, used at 1655, 1740, 1814, 1886, 1954) — a field read from the API payload but not stored in the table (identity PK). If Connect's v2 export omits or renames `id` (schema drift upstream), pages still **commit** (`conn.commit()` runs regardless) but the cursor never advances; after a mid-source failure, the next run resumes from the stale watermark and re-fetches already-committed rows into identity-PK tables that cannot dedupe → silent duplication, the f26c1a0 bug class reintroduced by upstream drift rather than by code. There is no assertion that a non-empty page yielded a usable max id.

- Status: LATENT · Impact: correctness (row duplication) · Confidence: hypothesis (depends on upstream payload guarantee I could not verify from this repo) · Complexity: accidental.

### F15 — COSMETIC: assorted small drift in the run lifecycle

- `_run_pipeline_with_progress.updater` (`tasks.py:489`) writes `progress` unconditionally, including onto terminal (FAILED/CANCELLED) runs.
- The cancel-before-transform branch (`materializer.py:439-441`) unconditionally overwrites `result`, clobbering whatever the external canceller wrote.
- The step-count guard (`materializer.py:491-495`) raises **after** the run is marked COMPLETED and the schema saved ACTIVE — a drift bug would report the tenant as failed (`tenant_results … success=False`) while the run row says COMPLETED, and the resume would call it "failed".
- `tool docstring` for `cancel_materialization` says "Marks the run as failed" — accurate to the code, but documents the drift (F5) as intended.

- Status: COSMETIC · Impact: correctness (minor) · Confidence: verified-by-trace.

### F16 — DEBT / test architecture: the refresh tests mock away exactly the seam that is broken

`tests/test_refresh_task.py` patches `apps.workspaces.tasks.run_pipeline` (line 73) and the registry, asserting only the state choreography around the call. The F1 wrong-schema load is therefore structurally invisible to the suite — same masking pattern project memory records for `aresolve_credential`/SynchronousOnlyOperation. Any fix to F1 should include a test that asserts *which schema* `run_pipeline` materializes into.

- Status: DEBT · Impact: velocity · Confidence: verified-by-trace.

---

## What's actually fine (verified)

- **Resume CAS chain** (`tasks.py:1033-1289`): claim CAS excluding RUNNING, CANCELLED-gets-one-resume, terminal CAS scoped to RUNNING so a concurrent cancel is never clobbered, post-aggregation as source of truth for the cancel-vs-complete race. This is careful, correct code; the 19-commit fix chain converged.
- **Materializer phase CAS transitions** (`materializer.py:240-244, 435-443, 471-477`): every transition filters on prior state so an external CANCELLED survives; pre-loop failure handler (`materializer.py:404-428`) guarantees a terminal state for every non-cancel failure.
- **Catalog reconciliation (#185)** (`metadata.py:29-112`): only `completed` sources whose physical table exists are surfaced; `in_progress` correctly hidden; `materialized_row_count` + `row_count_verified: False` labeling honest.
- **Resume-cursor eligibility (#187)** (`materializer.py:564-605`): most-recent-run rule correctly prevents stale-PARTIAL cursors from resurfacing after an intervening COMPLETED run; mutable `users` source correctly excluded from resume.
- **Janitor job-status lookup** (`tasks.py:693-725`): ORM read sidesteps the FutureApp pitfall; `None` = "don't touch this tick" semantics prevent transient-error misclassification.
- **Connection-hygiene task decorator** (`config/procrastinate.py`): close_old_connections before/after on the thread-sensitive executor, plus `close_old_connections()` inside `asyncio.to_thread` workers (`tasks.py:483`); explicitly temporary pending upstream #1555, enforced by test.
- **TTL incident-b fix for TenantSchema** (`schema_manager.py:114-122`): resurrect path resets `last_accessed_at`; janitor never auto-expires null `last_accessed_at`.
- **63-byte view-name guard (incident-1a fix)** (`schema_manager.py:219-350`): bounded prefixes with deterministic digests, collision checks on final names, pre-DDL length validation, idempotent DROP/recreate, error text persisted to `last_error`. Thorough.
- **HTTP cancel ordering** (`jobs_cancel.py`): DB flip before procrastinate abort, matching the worker's polling checkpoint; cross-user protection in `materialization_cancel_view` is well reasoned.
- **Sibling view-schema rebuild on rematerialization (#230)** (`tasks.py:398-463`): single-query, N+1-free, correctly excludes the current workspace (for its intended success-path purpose).
- **`expire_inactive_schemas` STALE-deferral design** (`tasks.py:516-553` + `teardown_schema`): flipping runs STALE only after the physical DROP succeeds, with the failed-DROP revert-to-ACTIVE path, is correct and well documented.
- **Pipeline registry** (`pipeline_registry.py`): simple, cached, fail-soft per-file; `get_by_provider` is the canonical mapping and both dispatch sites use it.

---

## Coverage log

**Deep-read (line by line):**
`apps/workspaces/tasks.py` (all 1,289 lines), `apps/workspaces/models.py`,
`mcp_server/services/materializer.py` (all 1,973 lines),
`apps/workspaces/services/schema_manager.py`, `mcp_server/server.py`,
`mcp_server/context.py`, `mcp_server/pipeline_registry.py`,
`mcp_server/services/metadata.py`, `apps/workspaces/api/materialization_views.py`,
`apps/workspaces/api/jobs_views.py`, `apps/workspaces/api/jobs_cancel.py`,
`apps/workspaces/api/views.py`, `apps/chat/models.py`,
`apps/workspaces/services/workspace_service.py`, `config/procrastinate.py`,
`mcp_server/loaders/connect_base.py`, `mcp_server/loaders/ocs_base.py`,
`mcp_server/loaders/commcare_base.py`, `apps/agents/mcp_client.py`.

**Skimmed (targeted sections / greps):**
`apps/agents/graph/base.py` (MCP_TOOL_NAMES, `_llm_tool_schemas`, injection node, `_build_tools`; NOT the prompt-assembly or escalation internals),
`apps/workspaces/api/workspace_views.py` (status derivation only, lines 40-130),
`apps/workspaces/management/commands/purge_synced_data.py`,
`frontend/src/store/dictionarySlice.ts`, `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx` (refresh wiring), `frontend/src/hooks/useWorkspaceJobs.ts` (poll interval only),
`tests/test_refresh_task.py`, `tests/test_threadjob_janitor.py` (grep-level).

**Not examined (in-scope but unopened — honest gaps):**
- Individual loader implementations beyond the three base classes: `connect_visits/users/completed_works/payments/invoices/assessments/completed_modules.py`, `ocs_sessions/messages/participants/experiments.py`, `commcare_cases/forms/metadata.py`, all three `*_metadata.py` loaders — pagination edge cases, field mappings, and the f26c1a0 page-replay fix details were not re-verified at the loader level.
- `mcp_server/services/query.py` and `sql_validator.py` (query execution / SET ROLE path).
- Transform phase internals: `apps/transformations/services/executor.py`, `dbt_runner.py`, `commcare_staging.py` — `_run_transform_phase` was traced only to its boundary.
- `apps/chat/views.py` / `stream.py` (how `touch_workspace_schemas` and chat dispatch interact beyond the one call site), `apps/agents/memory/checkpointer.py`.
- `pipelines/*.yml` contents (which sources are marked resumable/progress_unit in the actual YAML).
- The bulk of the test suite (`test_materializer.py`, `test_materialize_workspace_task.py`, `test_resume_thread_task.py`, `test_jobs_endpoints.py`, smoke tests) — F16 is from one file only; what the other mocks hide is unaudited.
- Frontend materialization UI beyond the two files above (MaterializationProgressBanner, WorkspaceJobsContext, retry button wiring).
- Deploy/worker concurrency config (`config/deploy-worker.yml`, Procfile) — relevant to F8's probability.
- `backfill_readonly_roles`, Django admin surfaces, migrations history.
