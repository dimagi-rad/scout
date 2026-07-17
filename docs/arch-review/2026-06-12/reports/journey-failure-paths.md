# Journey tracer: failure paths (worker death, cancel surfaces, credential expiry, provider drift, deploy-mid-job)

*Reviewer: journey:failure-paths · 2026-06-12 · branch `main` @ `35e4230`*
*Mandate: follow the user, not the module. For each failure journey: what does the user see, what does the operator see, what state is left behind.*

Evidence standards followed: every BROKEN-NOW claim carries a quoted entry-point→consequence chain; confidence labeled per finding; comments treated as claims, not facts.

---

## Journey 1 — Data Dictionary "Refresh" (the legacy `/refresh/` path)

### F1 · BROKEN-NOW · data-loss · verified-by-trace
**Refresh loads fresh data into the OLD active schema, then schedules that schema for destruction; the new schema it activates is empty.**

This confirms (and re-verifies, post-incident-fix-wave) the v1 run-A S1. Nothing in PRs #227–#232 touched it.

Chain (every hop read in this session):

1. **Entry**: Data Dictionary page renders a Refresh button — `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx:99` (`onClick={handleRefresh}`) → `frontend/src/store/dictionarySlice.ts:197` `await api.post(\`/api/workspaces/${activeDomainId}/refresh/\`)`.
2. `apps/workspaces/api/views.py:362-365` (`RefreshSchemaView.post`): `new_schema = SchemaManager().create_refresh_schema(tenant)` — a **new** `TenantSchema` row named `{sanitized}_r{hex8}` (`schema_manager.py:176`) — then `refresh_tenant_schema.defer(schema_id=..., membership_id=...)`.
3. `apps/workspaces/tasks.py:150` creates the physical `_r` schema for `new_schema`, then `tasks.py:173`:
   ```python
   await asyncio.to_thread(run_pipeline, membership, credential, pipeline_config)
   ```
   **`new_schema` is never passed to the pipeline.**
4. `mcp_server/services/materializer.py:183`: `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`. `provision()` looks up by the *standard* name `_sanitize_schema_name(tenant.external_id)` and **returns the existing ACTIVE schema** (`schema_manager.py:66-78`). All loaders therefore write into the **old** schema; the `_r` schema stays empty.
5. Back in the task on success, `tasks.py:182-184` marks the **empty** `_r` schema ACTIVE; `tasks.py:188-197` flips every *other* ACTIVE schema for the tenant — i.e. the schema that just received the fresh data — to TEARDOWN and defers `teardown_schema` with a 30-minute delay.
6. `tasks.py:620` → `schema_manager.py:198` `DROP SCHEMA IF EXISTS {} CASCADE` destroys the data, `tasks.py:639-645` marks its runs STALE, and `tasks.py:653` flips dependent multi-tenant view schemas to FAILED.

**End state**: the only ACTIVE schema for the tenant is empty and has no MaterializationRun, so `list_tables` returns `[]` with "Run run_materialization…" (`mcp_server/server.py:139-167`). All materialized data is gone (recoverable only by a full re-materialization from the provider). During the 30-minute grace window the result is nondeterministic: `load_tenant_context` picks between the two ACTIVE schemas with an unordered `.afirst()` (`mcp_server/context.py:56-59`).

**User sees**: refresh "succeeds" (202 → status `provisioning` → eventually `active` via `RefreshStatusView` reading the newest row), then 0–30 min later every table vanishes from chat, dictionary and artifacts. **Operator sees**: nothing — every step logged as success ("Refresh complete: schema '…_r…' is now active", `tasks.py:199`). **State left**: empty ACTIVE schema, EXPIRED old schema, STALE runs, FAILED dependent view schemas.

Side findings on the same path (all verified-by-trace):
- **F7 · DEBT · correctness**: the refresh run is *uncancellable and invisible to every cancel surface*: `run_pipeline` is invoked with no `progress_updater` (no cancellation checkpoint at all) and no `procrastinate_job_id` (`tasks.py:173`), so its `MaterializationRun` rows carry NULL job ids. `materialization_cancel_view` builds `job_ids = {r.procrastinate_job_id ... if not None}` (`materialization_views.py:56`); a NULL-id run can never enter the tracked or orphan branches, so the endpoint answers `no_active_run` while the run is demonstrably active.
- Docstring drift: `RefreshSchemaView` still says "dispatches a Celery task" (`views.py:319`) — Celery was removed 2026-05.

Complexity: accidental — two provisioning regimes (`create_refresh_schema` vs. `provision`) drifted; the materializer was rewritten around `provision()` and the refresh task was never migrated.

---

## Journey 2 — Worker dies mid-materialization (kill -9 / OOM / host crash / deploy timeout)

### F2 · BROKEN-NOW · correctness · verified-by-trace (code) / strong-inference (procrastinate semantics)
**A hard worker death leaves the procrastinate job in `doing` forever, and both janitors deliberately treat `doing` as "still running" — so the ThreadJob, the run row and the user's progress banner are stuck permanently. No stalled-job rescue exists anywhere.**

Chain:
1. Materialization runs inside `materialize_workspace` (worker process). Hard death ⇒ no Python code runs: `MaterializationRun` stays `LOADING` with frozen `progress`, `ThreadJob` stays `PENDING`, the procrastinate row stays `doing` (procrastinate only flips status from inside the worker; with the process gone, nothing does — same mechanism as the 2026-05-30 zombie incident).
2. Janitor (`expire_stale_thread_jobs`, cron */15) and the API-side poll backstop (`apps/workspaces/api/jobs_views.py:123-135`) both funnel into `reconcile_stale_thread_job` → `_procrastinate_job_status`, and:
   ```python
   if status in {"todo", "doing"}:
       return None                      # apps/workspaces/tasks.py:748-749
   ```
   `None` means "don't touch this row this tick" — correct for a *live* job, permanent for a *zombie* one. There is no heartbeat and no age escalation.
3. Repo-wide grep: no caller of procrastinate's stalled-job APIs (`retry_stalled_jobs` etc.) anywhere (`grep -rn stalled` matches only "installed").
4. On worker restart, procrastinate does not re-run `doing` jobs; the row stays `doing` indefinitely (strong-inference from procrastinate's design; consistent with the 2026-05-30 incident record).

**User sees**: the progress banner frozen at the last written percentage with the spinner animating forever (`MaterializationProgressBanner` renders whatever `MaterializationRun.progress` last said; the poll keeps returning the PENDING job). The chat turn never resumes. **Escape hatch**: the Stop button works — `cancel_thread_job` flips the run + ThreadJob to CANCELLED regardless of worker state (`jobs_cancel.py:30-41`), clearing the UI and offering Retry. Nothing tells the user to do this. **Operator sees**: nothing — no exception is ever raised, so no Sentry event; diagnosis requires SQL against `procrastinate_jobs`. **State left**: `procrastinate_jobs.status='doing'` forever, `MaterializationRun` in LOADING forever (no janitor ever touches run rows), `ThreadJob` PENDING forever unless the user cancels.

Note the asymmetry: the *dead-connection* failure mode (2026-06-09) is now well covered — the task decorator (`config/procrastinate.py:35-77`), janitor ORM-based status lookup (`tasks.py:693-725`), and the API-side backstop all handle a worker that is alive but failing. The *dead-process* failure mode is the remaining, older gap.

### Deploy-mid-job is the same finding with a schedule
`config/deploy-worker.yml` runs `cmd: "python manage.py procrastinate worker"` with no stop-timeout configuration. Kamal stops the old container with the default grace period before SIGKILL; procrastinate's graceful shutdown waits for the in-flight task, but a materialization routinely runs many minutes. Any worker deploy during a materialization therefore SIGKILLs it ⇒ Journey 2 state, on every such deploy. (Confidence: strong-inference on Kamal's default stop window; verified that no `stop_wait_time`/`stop_signal` is configured.)

What is fine on this journey: if the task *raises* instead of dying (e.g. DB blip at startup), procrastinate marks the job `failed`, and within ~10–15 min the janitor or the poll backstop flips the ThreadJob FAILED with a composed error summary and a synthetic chat message (`tasks.py:776-799`, `_persist_synthetic_failure_message`). That path is solid and tested.

---

## Journey 3 — User cancels, from each cancel surface

Inventory of cancel surfaces found:

| Surface | Route/tool | Verdict |
|---|---|---|
| Progress-banner **Stop** | `POST /api/workspaces/<ws>/jobs/<tj>/cancel/` → `cancel_thread_job` | **Correct** (see below) |
| Workspace-level cancel | `POST /api/workspaces/<ws>/materialization/cancel/` | Correct for tracked + orphan runs; **blind to NULL-job-id refresh runs (F7)**; no frontend caller found (HTTP-only surface) |
| Agent MCP tool `cancel_materialization` | `mcp_server/server.py:446` | **Semantically wrong + vestigial (F3)** |
| Chat stream Stop (SSE abort) | `ChatPanel.tsx:252 stop()` | Client-side abort only; background ThreadJob continues by design; resume still fires — coherent |

The good path, verified end-to-end: Stop → `cancel_thread_job` flips `MaterializationRun`→CANCELLED **before** the procrastinate abort (`jobs_cancel.py:26-44`, ordering documented and load-bearing because the worker's checkpoint reads DB state, `tasks.py:485-494`); the worker's `progress_updater` raises `MaterializationCancelled` at the next page; the in-flight source rolls back, earlier sources stay committed (`materializer.py:311-327, 387-402`); the resume task claims the CANCELLED ThreadJob (`tasks.py:1043`), and `_aggregate_materialization_state` — not the pre-CAS snapshot — decides the message, so a Stop that races completion truthfully reports "completed" (`tasks.py:1052-1061`). DISCOVER-phase cancels are caught by the DISCOVERING→LOADING CAS (`materializer.py:240-244`). This is genuinely well-engineered.

### F3 · LATENT · correctness · verified-by-trace (divergence), reachability limited
**The agent-facing `cancel_materialization` tool writes FAILED instead of CANCELLED, which no cancellation checkpoint detects — if invoked, the load runs to completion anyway, and the catalog then hides the fully-loaded data.**

- All 11 MCP tools are bound to the LLM: `_build_tools` does `tools = list(mcp_tools)` (`apps/agents/graph/base.py:692`) and `_llm_tool_schemas` passes tools outside `MCP_TOOL_NAMES` through **unchanged** (`base.py:408-410`) — so `cancel_materialization`, `get_materialization_status`, `list_pipelines` are all callable by the model.
- The tool sets `run.state = RunState.FAILED` with `result["cancelled"]=True` (`server.py:479-482`). The worker's checkpoint tests only `current_state == CANCELLED` (`tasks.py:493`), so loading continues, sources keep committing, and only the LOADING→TRANSFORMING CAS finally misses — which then **overwrites `result` with `{"cancelled": True, "sources": ...}`**, discarding pipeline/transform detail (`materializer.py:438-441`). The run terminates FAILED with all sources committed; `pipeline_list_tables` only surfaces tables for COMPLETED/PARTIAL runs (`server.py:148-156` pattern), so a "cancelled" run that actually loaded everything presents as *no data*. It also never aborts the procrastinate job nor touches the ThreadJob.
- Mitigating reachability: since the fire-and-ack redesign, no tool returns a `run_id` to the agent (`run_materialization` returns `thread_job_id`), so the model has no legitimate way to discover the argument; both `cancel_materialization` and `get_materialization_status` look like rename residue from the pre-background era. They confuse the tool surface at minimum.

Complexity: accidental — a sibling cancel path that drifted when the real cancel protocol (DB-state-first, CANCELLED) was built.

### F4 · LATENT · correctness · verified-by-trace
**A cancelled or partially-failed multi-tenant re-materialization leaves the workspace's own view schema ACTIVE but physically broken.**

- Every writer starts with `DROP TABLE IF EXISTS {}.raw_* CASCADE` (`materializer.py:851, 900, 961, 1017, 1122, 1189, 1419, 1514, ...`), which cascade-drops the namespaced views inside the **current** workspace's `ws_*` schema, not just siblings'.
- The rebuild runs only when `workspace_tenant_count > 1 and all_succeeded` (`tasks.py:322`); a cancel (`break` at `tasks.py:288`) or any tenant failure means `all_succeeded=False` ⇒ no rebuild. `_rebuild_sibling_view_schemas` explicitly excludes the current workspace (`tasks.py:434`), and `_fail_dependent_view_schemas` runs only from `teardown_schema` (`tasks.py:653`).
- The resume task's view-schema truth-check fires only for status `completed`/`partial` and only when `vs.state != ACTIVE` (`tasks.py:1075-1083`). Here state *is* ACTIVE — the row just lies.

**User sees**: after cancelling tenant B's load, the agent resumes ("status=cancelled… continue with the user's request"), calls `list_tables` through the still-ACTIVE view schema, and gets missing relations / a thinned `information_schema` listing for the tenant that *did* re-materialize; repeated NOT_FOUND errors then trip the panic-loop breaker, whose canned advice is "re-run materialization" (`base.py:87-94`) — which, by accident, is the correct repair (a full successful run rebuilds the views). **State left**: ACTIVE `WorkspaceViewSchema` row whose physical views for re-loaded tenants do not exist.

---

## Journey 4 — Credential expires mid-pipeline

### F5 · DEBT · correctness · strong-inference (plus one verified comment/code mismatch)
**Credentials are resolved once per tenant before the pipeline; there is no reactive refresh on 401, and a failed proactive refresh silently proceeds with the stale token.**

- Resolution: `aresolve_credential(tm)` per tenant at loop top (`tasks.py:264`); refresh happens only if the token expires within 5 minutes *at that moment* (`token_refresh.py:19, 43-51`).
- `token_refresh.py:4-5` claims refresh is "Called proactively (before token expires) and reactively (after 401)". Repo-wide grep shows exactly two call sites — `credential_resolver.py:106` and a login-time path in `auth_views.py:240`. **No reactive call exists.** Comment/code mismatch — itself a finding under the shared standards.
- All three loader families turn 401/403 into a provider AuthError (`connect_base.py:135-141, 198-202`, `ocs_base.py:47-52, 69-73`, `commcare_base.py:64-67`), which the per-source handler records as `state=failed` and skips the remaining sources (`materializer.py:328-371`).
- On refresh *failure*, the resolver logs a warning and returns the existing — likely expired — token (`credential_resolver.py:107-110`), guaranteeing a run whose every source 401s, instead of failing fast with "reconnect your account".

**User sees**: spinner → failure card with e.g. "visits failed: ConnectAuthError: Connect auth failed for opportunity 123: HTTP 401… remaining sources skipped" (via `_compose_failure_summary`) and a Retry button. Retry actually works well: by then the token is past expiry, `token_needs_refresh` is true, the refresh succeeds, and Connect sources resume from the per-page cursor watermark (`materializer.py:252-265`); CommCare/OCS restart from scratch (non-resumable). What's missing is any "re-authenticate" call to action when the refresh token itself is dead — the user just sees repeated 401 failure cards. **Operator sees**: warning log + per-source error in `result` — adequate. **State left**: PARTIAL run with preserved cursor; clean.

Severity is bounded by provider token lifetimes (unverified here): if access tokens outlive typical loads, this rarely bites; for multi-hour loads it is the expected failure mode.

---

## Journey 5 — Provider API changes shape

### F6 · LATENT · correctness (silent-empty) · verified-by-trace for the code, strong-inference for consequence
**Two of three provider families swallow envelope-shape drift as "zero rows, COMPLETED"; only Connect fails loud.**

- Connect: missing `results` key or non-JSON ⇒ `ConnectExportError` (`connect_base.py:217-223`) → source `failed`, run PARTIAL/FAILED, error surfaced to user and operator. Bounded urllib3 retries with upstream sentry-trace capture (`connect_base.py:36-37, 203-215`, consumed at `tasks.py:289-306`). This is the gold standard in this codebase.
- OCS: `page = payload.get("results", [])` (`ocs_base.py:75`) — a renamed key yields an empty page list and a clean loop exit; `ocs_messages.py:50` `detail_resp.json().get("messages") or []` and `:41` `session.get("id")` likewise. Result: source `completed`, `rows: 0`, run COMPLETED.
- CommCare: `data.get("cases", [])` (`commcare_cases.py:52`) — same silent-empty class; `_normalize_case` `.get(..., "")` for every field means renamed *fields* become empty strings rather than errors.

**User sees**: materialization "completed", catalog lists tables with 0 rows (or rows full of empty strings), agent confidently reports "there is no data" / wrong values. **Operator sees**: a successful run; nothing to alert on. **State left**: COMPLETED run rows asserting a truth that is upstream drift. Also inconsistent transport hardening: Connect has a retry adapter; OCS and CommCare sessions have none — a transient 502 mid-OCS-load fails the source outright.

Sibling exposure: the OCS/CommCare writers (`_write_ocs_*`, `materializer.py:841-1058`; CommCare writers `:1122+`) were not audited for the missing-id crash class fixed for Connect (`2587158`) — flagged as a coverage gap, not a finding.

---

## Cross-journey observations

1. **The cancel/resume spine (chat-initiated, worker-executed) is the most-fixed and now genuinely strongest part of the system** — CAS at every transition, DB-state-before-abort ordering, truthful aggregate status, dual janitor/backstop. The failures that remain live exactly where that machinery *isn't*: the legacy refresh path (F1/F7), the dead-process gap (F2), the vestigial agent cancel (F3), and the view-schema edge of the multi-tenant matrix (F4).
2. **"Status unknown ⇒ do nothing" is correct per-tick and wrong forever.** `reconcile_stale_thread_job` needs an age-based escalation for `doing` (e.g. `doing` + no progress-row write for N minutes ⇒ treat as crashed), or a wired `retry_stalled_jobs`.
3. **Operator visibility is exception-shaped.** Every journey where the process dies (rather than raises) emits nothing. There is no metric/alarm on: jobs `doing` older than X, ThreadJobs active older than X, ACTIVE schemas with zero tables, ACTIVE view schemas whose physical views are missing.
4. Essential vs accidental: almost everything above is accidental complexity — drifted siblings of mechanisms that were later rebuilt properly (refresh vs. provision; tool-cancel vs. endpoint-cancel; current-workspace vs. sibling view rebuild). The essential complexity (multi-phase pipeline with per-source commit and resume cursors) is handled well where it was rebuilt.

## What's fine (verified healthy)

- `cancel_thread_job` ordering + worker checkpoint + resume CAS chain (`jobs_cancel.py`, `tasks.py:1043-1061, 1258-1288`).
- Materializer phase-transition CAS set; per-source commit isolation; cursor watermarks; PARTIAL semantics; honest progress denominators (`materializer.py` throughout).
- Janitor + API-side reconcile for `failed`/`aborted` procrastinate jobs, with synthetic user-visible failure messages (`tasks.py:728-816`, `jobs_views.py:123-135`).
- Worker dead-connection hygiene decorator with test-enforced registration (`config/procrastinate.py`, "tests/test_worker_db_resilience.py" per its docstring).
- Dangling tool-call repair in `agent_node` protects checkpoints across API restarts mid-stream (`base.py:549-578`).
- `run_materialization` tool: ownership checks, in-flight dedupe, dispatch rollback on tracking failure (`server.py:545-648`).
- Connect loader hardening: bounded retries, sentry-trace propagation, loud envelope validation, http→https `next`-URL handling.

## Coverage log

**Deep-read**: `apps/workspaces/tasks.py` (entire); `apps/workspaces/api/materialization_views.py`; `apps/workspaces/api/jobs_cancel.py`; `apps/workspaces/api/jobs_views.py`; `config/procrastinate.py`; `mcp_server/server.py` (entire); `mcp_server/services/materializer.py` lines 1–510 + grep map of all writer DROP/CREATE sites; `apps/workspaces/services/schema_manager.py` lines 1–245; `apps/users/services/credential_resolver.py`; `apps/users/services/token_refresh.py`; `mcp_server/context.py`; `mcp_server/loaders/connect_base.py`, `ocs_base.py`, `commcare_base.py`, `ocs_messages.py`; `apps/chat/models.py`; `apps/agents/mcp_client.py`; `apps/agents/graph/base.py` (lines 55–115, 390–610, 668–725); `apps/workspaces/api/views.py` lines 300–460; `frontend/src/hooks/useWorkspaceJobs.ts`; `frontend/src/components/MaterializationStatus/MaterializationProgressBanner.tsx`; `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx` (lines 1–105); `config/deploy-worker.yml`.

**Skimmed**: `apps/chat/stream.py` (header/timeout only); `mcp_server/loaders/commcare_cases.py` (grep); `frontend/src/store/dictionarySlice.ts` (grep); `pyproject.toml`; git history around 2026-05-30 and zombie/orphan fix commits.

**Not examined** (honest gaps for the gap loop):
- The other ~13 loaders in detail (`connect_visits/users/payments/...`, `ocs_sessions/participants/experiments`, `commcare_forms/metadata`) — id semantics, dedup-on-replay siblings of `f26c1a0`/`2587158`.
- Materializer writer bodies lines 510–1972 beyond grep (OCS/CommCare writer id handling, `_run_transform_phase`/dbt failure isolation).
- `schema_manager.build_view_schema` body (lines 245–449) and teardown_view_schema.
- `apps/chat/views.py` and `thread_views.py` in full (client-disconnect mid-SSE handling asserted only from the dangling-tool-call repair).
- `config/deploy.yml` (API/MCP deploy plane), Kamal stop_wait defaults (not verified against Kamal docs), procrastinate library source (doing-job semantics inferred, not read).
- `expire_inactive_schemas` vs. provision TTL race (covered by PRs #227–#232; fixes not re-verified here).
- Recipes runner and artifacts failure paths; `MaterializationFailure.tsx` / retry UX detail; `tests/qa` scenarios; `workspace_service.py`; MCP `envelope.py` / `tool_context` internals.
- Whether any external client still calls `materialization_cancel_view` (no frontend caller found; did not audit QA scripts/ops tooling).
