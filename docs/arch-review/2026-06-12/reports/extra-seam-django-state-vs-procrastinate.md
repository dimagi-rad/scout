# Seam review: Django job-state rows ↔ procrastinate internals

*Reviewer: extra-seam (Django state vs. procrastinate queue library). 2026-06-12.*
*Scope: every read/write of procrastinate internals, job-lifecycle assumptions in
`ThreadJob`/`MaterializationRun`, behavior on worker death, deploy-mid-job, and library
upgrade. Installed library: procrastinate **3.8.1** (`uv.lock:2003`), declared as
`procrastinate[django]>=0.28` (`pyproject.toml:34`, lower bound only).*

---

## 1. The contract as built

### 1.1 Every touch-point with procrastinate internals (exhaustive, non-test)

| Site | What it does | Mechanism |
|---|---|---|
| `config/procrastinate.py:35-77` | custom `task` decorator wrapping `app.task`; close_old_connections before/after each task body | `procrastinate.contrib.django.app` (the safe lazy `ProxyApp`) |
| `apps/workspaces/tasks.py:15,693-725` | `_procrastinate_job_status` — janitor reads raw `status` from `procrastinate_jobs` | `procrastinate.contrib.django.models.ProcrastinateJob` ORM model |
| `apps/workspaces/tasks.py:748,776` | janitor interprets raw status strings `{"todo","doing"}`, `{"failed","aborted"}` | hand-rolled string sets, not `procrastinate.jobs.Status` |
| `mcp_server/server.py:32,633` | rollback-abort when `ThreadJob.acreate` fails after `defer_async` | `from procrastinate.contrib.django.procrastinate_app import current_app as _procrastinate_app` (import-time binding) |
| `apps/workspaces/api/jobs_cancel.py:11,44` | cancel: `cancel_job_by_id_async(id, abort=True)` | same import-time `current_app` binding |
| `apps/workspaces/api/materialization_views.py:9,108,209` | orphan-run abort + retry rollback-abort | same import-time `current_app` binding |
| `apps/workspaces/tasks.py:221` (`context.job.id`), `mcp_server/server.py:607,615`, `materialization_views.py:185,192` | capture the integer job id at defer / inside the task | `pass_context=True`, `defer_async` return |
| `apps/chat/models.py:75` | `ThreadJob.procrastinate_job_id` BigInt **unique, NOT NULL** | foreign reference into `procrastinate_jobs` with no FK |
| `apps/workspaces/models.py:92` | `MaterializationRun.procrastinate_job_id` BigInt **nullable** | same, written by `mcp_server/services/materializer.py:186-190` |
| `apps/workspaces/tasks.py:516,819` | `@app.periodic(cron=...)` for both janitors | periodic deferrer (runs inside the worker) |

Nothing in the repo calls `get_stalled_jobs`, `retry_job`, `prune_stalled_workers`, or
schedules the builtin `remove_old_jobs` (verified by grep over `apps/`, `config/`,
`mcp_server/`).

### 1.2 Lifecycle assumptions encoded in Scout

- `ThreadJob.procrastinate_job_id` always points at the **materialize_workspace** job
  (`server.py:626`, `materialization_views.py:201`). The chained
  `resume_thread_after_materialization` job's id is **never recorded**
  (`tasks.py:393` defers it and discards the returned job).
- Janitor semantics (`reconcile_stale_thread_job`, `tasks.py:728-816`): status `None` =
  "don't touch this tick"; `todo|doing` = still active; `failed|aborted` = flip PENDING
  → FAILED; **anything else** (`succeeded`, `cancelled`, and any unknown/future string)
  = "safe to act" (flip RUNNING → FAILED, or defer a fresh resume for PENDING).
- Staleness = `ThreadJob.created_at < now - 10min` (`tasks.py:690`,
  `jobs_views.py:123-127`) — anchored to **dispatch time**, not to the last state
  transition (`ThreadJob` has no `started_at`/`updated_at` column, `chat/models.py:72-85`).
- Cancellation: DB rows flipped first, then best-effort
  `cancel_job_by_id_async(abort=True)` (`jobs_cancel.py:24-52`). The task body never
  checks `context.should_abort`; for async tasks procrastinate 3.8 instead **cancels the
  asyncio task** (`procrastinate/worker.py:471-472`), and the real cancellation
  checkpoint is the DB re-read in `progress_updater` (`tasks.py:485-494`).
- Worker topology: one container, `python manage.py procrastinate worker` with **no
  flags** (`config/deploy-worker.yml:16`) → concurrency 1
  (`procrastinate/worker.py:28: WORKER_CONCURRENCY = 1`), no
  `--shutdown-graceful-timeout` (default `None` = wait forever,
  `procrastinate/worker.py:43,496-514`), no `--delete-jobs`.

---

## 2. Findings

### F1 — Stale-job reconciler falsely fails *live* resumes; 3-second poll backstop makes it near-deterministic for slow materializations
**Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

The reconciler answers "is the resume worker still alive?" by reading the status of the
**wrong procrastinate job**, and measures staleness from a timestamp that includes the
entire materialization + queue time.

Chain:

1. Dispatch: `mcp_server/server.py:607` defers `materialize_workspace`; `:623-629`
   creates `ThreadJob(procrastinate_job_id=<materialization job>, state=PENDING)` —
   `created_at` = now (`chat/models.py:78`, auto_now_add).
2. Materialization runs for M minutes (single-slot worker, so M also includes any time
   queued behind other workspaces' jobs — see F6). On finish, the resume task is
   deferred (`tasks.py:356-360,393`); **its job id is not recorded anywhere**.
3. Resume claims the row: `tasks.py:1044-1047` CAS PENDING→RUNNING, then
   `agent.ainvoke` for up to `AGENT_RESUME_TIMEOUT_S` = 120s (`tasks.py:1127,1161-1164`).
4. Frontend polls `GET /jobs/active/` every 3s
   (`frontend/src/hooks/useWorkspaceJobs.ts:4`). The poll backstop reconciles any
   active ThreadJob with `created_at < now - 10min`
   (`apps/workspaces/api/jobs_views.py:123-129` → `tasks.py:728`).
5. `_procrastinate_job_status(tj.procrastinate_job_id)` (`tasks.py:742`) returns
   `"succeeded"` — that's the **materialization** job, which by definition has finished
   whenever a resume is running. Not in `{"todo","doing"}` (`tasks.py:748`), so the
   RUNNING branch fires: CAS RUNNING→FAILED (`tasks.py:754-764`) and a synthetic
   AIMessage *"…the follow-up response was interrupted (likely a server restart).
   Please re-ask your question."* is written into the thread checkpointer
   (`tasks.py:774`, `RESUME_STUCK_RUNNING_MESSAGE` `tasks.py:53-56`).
6. The genuinely-live resume finishes its `ainvoke` (the real answer **is** persisted by
   LangGraph), then its terminal CAS on `state=RUNNING` matches zero rows and it backs
   off (`tasks.py:1264-1288`).

Consequence: for any materialization where dispatch→resume-start exceeds ~10 minutes
(big Connect/OCS pulls; or *any* size when queued behind another workspace's run —
F6), the first poll after the resume claims RUNNING flips the job to FAILED, shows the
failure card + retry affordance, and injects a false "server restart" message that
interleaves with the real answer. The worker-side janitor (`*/15` cron,
`tasks.py:819-838`) does the same thing with coarser timing. Tests pin this behavior
only for `created_at` hours in the past (`tests/test_threadjob_janitor.py:175-263`) —
the "resume legitimately still running" case is untested.

The mechanism cannot be fixed by tuning the threshold: the row carries no timestamp for
the PENDING→RUNNING transition and no reference to the resume job, so the reconciler
has literally no signal that distinguishes "resume crashed" from "resume in flight".
(Either recording the resume's procrastinate job id, or stamping the RUNNING
transition, closes it.)

Reachable via: chat → `run_materialization` tool → any open tab polling
`/jobs/active/`. Introduced by the 2026-06-09 incident fix `e91ff9b` (the API-side
backstop); the janitor-side variant predates it.

### F2 — Worker death mid-job still produces permanent `doing` zombies; nothing in the stack rescues them; the documented docker-compose deployment has **no worker at all**
**Status: LATENT (BROKEN-NOW for the docker-compose surface) · Impact: correctness · Confidence: verified-by-trace · Complexity: mixed**

The 2026-05-30 fix (`28b6647`) only repaired the janitor's *status lookup* (FutureApp →
ORM). The zombie class itself — procrastinate job wedged in `doing` after the worker is
killed — is still unhandled:

- The janitor deliberately treats `doing` as "still active" forever
  (`tasks.py:748-749`); the API poll backstop runs the same function
  (`jobs_views.py:129`), so a ThreadJob whose job is a `doing` zombie stays PENDING and
  the UI shows "Preparing…" indefinitely. The user's only exit is the Stop button
  (`cancel_thread_job` flips DB rows regardless of worker health).
- Procrastinate 3.8 has the primitives to detect this (worker heartbeats,
  `get_stalled_jobs(seconds_since_heartbeat)`, `procrastinate/manager.py:223-270`),
  but nothing in Scout calls them; `prune_stalled_workers` only deletes worker rows,
  it does not requeue their jobs.
- Deploy-mid-job is a guaranteed producer: the worker has no
  `--shutdown-graceful-timeout`, so on SIGTERM it waits for running jobs indefinitely
  (`procrastinate/worker.py:496-514`); a materialization that outlives the container
  runtime's kill grace (no override in `config/deploy-worker.yml`) ends in SIGKILL →
  `doing` zombie.
- A second un-reconciled surface: **MaterializationRun rows have no janitor at all.**
  A SIGKILL between `MaterializationRun.objects.create(state=DISCOVERING)`
  (`materializer.py:186-190`) and any terminal write leaves the run in an ACTIVE state
  forever; `get_materialization_status` (`mcp_server/server.py:408-438`) will report it
  as in-flight to the agent indefinitely, and the cancel endpoint keeps listing it.
- The in-repo self-host stack is the degenerate case: `docker-compose.yml` defines
  `platform-db`, `mcp-server`, `api`, `frontend` — **no procrastinate worker service**
  — while `CLAUDE.md` advertises `docker compose up` as "All services". Every deferred
  task (materializations, resumes, *both janitors*) sits in `todo` forever; the
  reconciler returns `None` for `todo` and never flips anything. This was called out in
  the 2026-05-29 investigation (`~/Code/dimagi/scout-materialization-orphan-jobs.md`,
  "Fix: add a worker service") and is still true at HEAD.

Reachable via: any deploy/OOM/host failure during a long materialization (twice in
prod already: 2026-05-29 job 1933, 2026-06-09); docker-compose path broken for every
materialization.

### F3 — The dead-connection fix does not cover ORM calls made on `asyncio.to_thread` executor threads; June-9 failure class survives on the view-schema and refresh paths
**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

The temporary decorator runs `close_old_connections()` via
`sync_to_async(..., thread_sensitive=True)` (`config/procrastinate.py:31-32`), i.e. on
asgiref's single thread-sensitive executor thread — the one the async ORM uses. Django
connections are thread-local; code that touches the ORM from a *different* thread pool
gets its own connection that the decorator can never clean. The codebase knows this:
`_run_pipeline_with_progress` opens with exactly this explanation and its own
`close_old_connections()` (`tasks.py:478-483`).

But two sibling paths run ORM on default-executor threads with **no** such call:

- `await asyncio.to_thread(SchemaManager().build_view_schema, workspace)` —
  `tasks.py:324` (post-materialization) and `tasks.py:571`
  (`rebuild_workspace_view_schema`, also the sibling-rebuild fan-out target).
  `build_view_schema` is ORM-heavy on that thread:
  `schema_manager.py:265` (TenantSchema filter), `:280` (get_or_create), `:287,430,438`
  (saves), `:306` (Tenant.objects.get).
- `await asyncio.to_thread(run_pipeline, membership, credential, pipeline_config)` —
  `tasks.py:173` (`refresh_tenant_schema`). `run_pipeline` creates/updates
  `MaterializationRun` via the ORM throughout (`materializer.py:186,240,393,471`).
  Unlike the materialize path, no `close_old_connections()` wrapper.

After an RDS restart, any pool thread holding a dead connection poisons whichever of
these lands on it; the failure surfaces as `state=FAILED` view schemas
("system-side fix required" messages to the agent, `tasks.py:1085-1095`) or failed
refreshes, intermittently, until the thread happens to host a pipeline run (which
heals it). The janitors themselves are healed by the decorator, so this is a partial,
non-22h variant of the same incident class — exactly the kind of sibling the
"temporary pending upstream fix" comment doesn't cover (and upstream PR #1555 won't
cover either, since it cleans the same thread the decorator does).

### F4 — Import-time `current_app` binding: the exact pattern that broke the janitor (fixed `28b6647`) survives at three call sites, all with swallowed exceptions
**Status: DEBT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`_procrastinate_job_status`'s docstring (`tasks.py:697-712`) documents the failure: a
module-level `from procrastinate.contrib.django.procrastinate_app import current_app`
binds the pre-ready `FutureApp` Blueprint if the import happens before
`ProcrastinateConfig.ready()` rebinds the module attribute
(`procrastinate/contrib/django/procrastinate_app.py:67-74`,
`contrib/django/apps.py:17-19`); `FutureApp` has no `job_manager`, so every call raised
`AttributeError` — and the janitor silently reconciled nothing.

The same import-by-value pattern remains at:
- `apps/workspaces/api/jobs_cancel.py:11` (used `:44`)
- `apps/workspaces/api/materialization_views.py:9` (used `:108,209`)
- `mcp_server/server.py:32` (used `:633`)

These work *today* only because of import timing (Django URLconf loads views after
`ready()`; `mcp_server/__main__.py:13` calls `django.setup()` before importing
`server`). Every call site wraps the abort in `try/except`-warn or
`contextlib.suppress`, so a future import-order change (e.g. a tasks module importing
the cancel helper — the natural refactor, since `tasks.py` and `jobs_cancel.py` share
cancel semantics) would silently turn every abort/rollback-abort into a no-op with at
most a log line. The library ships the safe spelling
(`from procrastinate.contrib.django import app` — the lazy `ProxyApp`, already used by
`config/procrastinate.py:12`); these three sites should use it.

### F5 — Cancel endpoint's comment claims the orphan path covers `/refresh/` runs; the code filters them out (NULL `procrastinate_job_id`)
**Status: DEBT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`materialization_views.py:60-62`: *"The orphan path below covers untracked runs (e.g.
/refresh/-triggered jobs with no ThreadJob)"*. But:

- `refresh_tenant_schema` calls `run_pipeline` with no `procrastinate_job_id`
  (`tasks.py:173`), so its `MaterializationRun.procrastinate_job_id` is NULL
  (`materializer.py:186-190`, model `workspaces/models.py:92`).
- The cancel view builds `job_ids` excluding None (`materialization_views.py:56`);
  orphan handling operates only on `job_ids - all_tracked_job_ids` (`:93,101`).
- Result: a refresh-path run in an ACTIVE state is listed in `active_runs` but can
  never be flipped CANCELLED; if it is the only active run the endpoint returns
  `{"status": "no_active_run"}` (`:119-120`) while a run is in fact active. The
  refresh path also passes no `progress_updater`, so there is no cooperative
  cancellation checkpoint at all — refresh runs are uncancellable end-to-end.

Comment/code mismatch per the evidence standards. Reachable via
`POST /api/workspaces/<id>/refresh/` (`api/views.py:365`) + the cancel button.

### F6 — Platform-wide serialization: one worker × concurrency 1; queue wait feeds the F1 staleness anchor
**Status: DEBT · Impact: cost-perf · Confidence: verified-by-trace · Complexity: accidental**

`deploy-worker.yml:16` runs `procrastinate worker` with no `--concurrency`; the
library default is 1 (`procrastinate/worker.py:28`). Every background unit —
materializations for all workspaces, all resume tasks, both janitor crons, view-schema
rebuilds and the sibling-rebuild fan-out (`tasks.py:441-463`) — shares one slot. A
single long materialization therefore: (a) delays every other workspace's
materialization and resume by its full duration; (b) delays the janitors (periodic
ticks queue as `todo` behind it); (c) inflates `created_at`-anchored staleness for
*queued* jobs, so a short materialization that waited >10 min in `todo` enters F1's
false-fail window the moment its resume starts. `_defer_resume_for_job`'s own comment
acknowledges the single-slot model (`tasks.py:368-370`). The decorator + tasks are
already written async-safe, so raising concurrency is plausible — but note the
materializer's per-table writers and dbt runner lock were sized for one pipeline at a
time (out of my seam; flagging for the materialization vertical).

### F7 — No queue-table hygiene, and the janitor's contract quietly depends on its absence
**Status: DEBT · Impact: cost-perf · Confidence: verified-by-trace · Complexity: accidental**

The worker is not started with `--delete-jobs`, and the builtin `remove_old_jobs`
periodic task is never scheduled (grep: zero references outside the library), so
`procrastinate_jobs`/`procrastinate_events` grow without bound — every chat
materialization adds rows forever. The flip side: `_procrastinate_job_status` returns
`None` for an id that no longer exists ("also returned for an unknown job id",
`tasks.py:710-711`), and `None` means "never touch the row" — so if an operator ever
prunes the queue table, any still-active ThreadJob referencing a pruned id becomes
permanently un-reconcilable (spinner until manual cancel). Retention and reconciliation
need to be designed together; today the system is only correct because nothing is ever
deleted.

### F8 — Janitor status interpretation is an open-ended allowlist over raw strings; unknown statuses fall into the *act* branch; dependency is lower-bound-only
**Status: LATENT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`reconcile_stale_thread_job` hard-codes `{"todo","doing"}` → skip and
`{"failed","aborted"}` → fail (`tasks.py:748,776`); **every other** string — including
statuses that don't exist yet — falls through to "defer a fresh resume"
(`tasks.py:800-804`). The library's enum still carries `ABORTING` ("legacy, not used
anymore", `procrastinate/jobs.py:55`); in procrastinate 2.x that status meant "abort
requested, job still running" — under this code it would defer a resume against a
running job and rely on the CAS to contain the damage. With `procrastinate[django]>=0.28`
as the only constraint (`pyproject.toml:34`), `uv lock --upgrade` can cross majors
silently; this seam has already absorbed one semantic change (2.x→3.x abort model:
async jobs are now cancelled via `asyncio.Task.cancel`, `procrastinate/worker.py:471-472`
— which Scout's "abort only fires at the next await" comments at `jobs_cancel.py:26`
predate but happen to survive, because the DB-flip-first ordering and the `finally`
resume-defer at `tasks.py:356-360` both tolerate CancelledError). Defensive fixes are
cheap: compare against `procrastinate.jobs.Status`, default unknown → `None`-skip, and
pin an upper bound.

### F9 — Acknowledged dispatch race: ThreadJob committed after `defer_async`; truly orphaned jobs are invisible to every janitor
**Status: DEBT · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

Known and TODO-documented (`tasks.py:373-376`, `server.py:617-621`): MCP defers the job
first, then creates the ThreadJob; the worker hedges with a ~3.75s backoff
(`tasks.py:380-383`) and the janitor catches the slow case at up to ~25 min latency
(15-min cron + 10-min threshold). Two residual holes worth recording beyond the TODO:
(a) if `ThreadJob.acreate` fails *and* the rollback abort fails (both guarded by
swallow-and-log, `server.py:630-635`), the materialization runs with **no ThreadJob**
— `expire_stale_thread_jobs` iterates ThreadJobs only (`tasks.py:828-835`), so nothing
ever reconciles it (its MaterializationRuns reach terminal states, but no resume fires
and no user-visible record exists outside the cancel view's orphan listing); (b) the
unique NOT-NULL `procrastinate_job_id` (`chat/models.py:75`) is the stated reason the
clean fix (pre-create placeholder, patch id after dispatch) was skipped — the
migration is small and would delete this whole class.

---

## 3. What's fine (verified)

- **CAS discipline on ThreadJob** is genuinely sound: claim excludes RUNNING with the
  documented matched-not-changed rationale (`tasks.py:1037-1047`); terminal write is
  scoped to `state=RUNNING` and re-reads on miss instead of clobbering a concurrent
  cancel (`tasks.py:1258-1288`); janitor flips are CAS-scoped too (`tasks.py:754-757,
  782-785`). Duplicate-`ainvoke` protection holds under every interleaving I traced.
- **`_procrastinate_job_status` None-semantics** ("couldn't tell ≠ not active",
  `tasks.py:706-712`) is the right default and is honored by both callers.
- **Cancel ordering** (DB flip before abort signal, `jobs_cancel.py:24-26`) is correct
  for work running inside `asyncio.to_thread`, and the `finally`-deferred resume
  (`tasks.py:356-360`) survives both abort-cancellation and early returns.
- **Teardown/STALE ordering**: runs flip STALE only after the physical DROP succeeds,
  with the failure path restoring ACTIVE (`tasks.py:609-663`) — the
  data-stranding hazard is reasoned about in-code and handled.
- **The connection-hygiene decorator itself** (`config/procrastinate.py`) mirrors
  Django's request lifecycle on the correct (thread-sensitive) thread, is enforced by
  `tests/test_worker_db_resilience.py` over every task in `apps.workspaces.tasks`, and
  carries a tracked removal plan (upstream #1134/#1555, repo issue #225). Within its
  thread, it is correct (gap is F3, a different thread).
- **Fire-and-ack authz**: `run_materialization` re-validates workspace membership and
  thread ownership before binding a ThreadJob (`server.py:553-570`).

## 4. Upgrade-readiness summary (mandate item)

Coupling points that must be re-checked on any procrastinate upgrade: contrib
`ProcrastinateJob` model/table shape (janitor reads it raw), `Status` enum strings
(F8), `current_app`/`FutureApp` ready() mechanics (F4), `cancel_job_by_id_async`
signature + async-abort semantics (worker now cancels asyncio tasks), periodic-deferrer
behavior under a saturated single slot (F6), and the decorator-removal condition
(#225 — after which `tests/test_worker_db_resilience.py`'s registration check must be
retargeted at the upstream mechanism rather than deleted).

## 5. Coverage log

**Deep-read (line-by-line):** `apps/workspaces/tasks.py` (all 1,289 lines),
`config/procrastinate.py`, `apps/chat/models.py`, `apps/workspaces/models.py`,
`apps/workspaces/api/jobs_views.py`, `apps/workspaces/api/jobs_cancel.py`,
`apps/workspaces/api/materialization_views.py`, `mcp_server/server.py:400-700`
(run_materialization, get_materialization_status, status tools),
`config/deploy-worker.yml`, procrastinate 3.8.1 sources (`worker.py` abort/shutdown
paths, `contrib/django/procrastinate_app.py`, `contrib/django/apps.py`, `jobs.py`
Status enum, `manager.py` stalled/heartbeat API, `sql/queries.sql` prune/stalled).

**Skimmed:** `apps/workspaces/services/schema_manager.py` (ORM/connection survey +
teardown body), `mcp_server/services/materializer.py` (MaterializationRun state writes
only, via grep + spot reads), `tests/test_threadjob_janitor.py`,
`tests/test_worker_db_resilience.py` (headers + assertions),
`docker-compose.yml` (service list), `Procfile.dev`, `tasks.py` (repo root — invoke
shortcuts, not a Django module), `frontend/src/hooks/useWorkspaceJobs.ts` (poll
interval only), `~/Code/dimagi/scout-materialization-orphan-jobs.md` (prior incident
report, used as steering only — all claims re-verified against HEAD).

**Not examined:** `mcp_server/services/materializer.py` beyond run-state writes (1,972
lines — loaders, cursors, catalog reconciliation belong to the materializer vertical);
`apps/agents/graph/base.py` and the checkpointer (resume's `ainvoke` internals taken as
a black box); `apps/chat/views.py` / streaming; `apps/workspaces/api/views.py` refresh
endpoint beyond the defer call; TaskBadger integration (`config/taskbadger.py`) —
unexamined despite being a third observer of task lifecycle; Kamal's actual stop/kill
timeout behavior (asserted only as "no override present", not traced into kamal);
procrastinate periodic-deferrer catch-up semantics under long worker outages
(missed cron ticks); `tests/test_resume_thread_task.py` (1,016 lines);
`mcp_server/context.py` touch paths; frontend rendering of failure cards.
