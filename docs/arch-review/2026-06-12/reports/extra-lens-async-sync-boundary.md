# Lens report: async/sync boundary & DB-connection lifecycle

*Reviewer: extra-lens-async-sync-boundary (architecture review v2, Phase 1).
Date: 2026-06-12. Repo HEAD: 35e4230.*

Mandate: hunt one failure class everywhere — async/sync crossings and DB-connection
lifecycle across the three connection regimes (Django ORM async/sync, AsyncConnectionPool
for the LangGraph checkpointer, psycopg-direct in the MCP server/materializer), in all
processes (API/uvicorn ×4 workers, procrastinate worker, MCP server).

## Method

- Read the cartography map, then deep-read every file that crosses the boundary:
  `config/procrastinate.py`, `apps/workspaces/tasks.py` (all 1,289 lines),
  `apps/chat/{views,checkpointer,helpers,stream,thread_views,rate_limiting}.py`,
  `apps/workspaces/services/schema_manager.py` (full), `mcp_server/{context,server(part)}.py`,
  `mcp_server/services/{query,materializer(parts)}.py`, `apps/users/{views,auth_views(part),
  decorators}.py`, `apps/users/services/{credential_resolver,token_refresh}.py`,
  `apps/recipes/services/runner.py`, `apps/agents/{mcp_client,graph/base(part)}.py`,
  `apps/workspaces/{api/jobs_views,services/workspace_service,workspace_resolver}.py`.
- Ran an AST scan over all 142 `async def` functions in `apps/`, `mcp_server/`, `config/`
  looking for sync ORM calls (`.objects.get/create/save/...` without the `a` prefix):
  **zero hits**. (Caveat: the scan cannot see lazy FK attribute loads or sync queryset
  iteration; those were checked by hand at the known hot spots.)
- Verified settings: `DATABASES` from `env.db("DATABASE_URL")` (`config/settings/base.py:120-122`)
  → `CONN_MAX_AGE` defaults to 0 and `CONN_HEALTH_CHECKS` is never set anywhere
  (grep over `config/settings/*.py` — no override in production.py).
- Checked the installed procrastinate (3.8.1) for stalled-job APIs.

## The three connection regimes, as built

| Regime | Where | Hygiene mechanism | Status |
|---|---|---|---|
| Django ORM (async + sync) in **API process** | all views | per-request `close_old_connections` via request signals + `ThreadSensitiveContext` per request; `CONN_MAX_AGE=0` | healthy |
| Django ORM in **worker process**, async-ORM thread | every `@task` body | custom decorator `config/procrastinate.py:35-77` (`close_old_connections` before/after on the thread-sensitive executor), enforced by `tests/test_worker_db_resilience.py` | healthy — but explicitly temporary (upstream procrastinate #1134 / PR #1555, issue #225) |
| Django ORM in **worker process**, `asyncio.to_thread` pool threads | `run_pipeline`, `build_view_schema` | `close_old_connections()` only at `tasks.py:483` (`_run_pipeline_with_progress`) | **two of three call sites unprotected — Finding 2** |
| Django ORM in **MCP server process** | every tool call (`context.py`, `server.py`) | **none** | **Finding 1** |
| AsyncConnectionPool (checkpointer) | API + worker (`apps/chat/checkpointer.py`) | unsynchronized lazy singleton; recovery = `force_new` (close & rebuild whole pool) | **Finding 3** |
| psycopg-direct | `schema_manager.py`, `materializer.py`, `mcp_server/services/query.py`, `metadata.py` | fresh connection per operation, context-managed / try-finally close | healthy (cost noted, Finding 8) |

---

## Findings

### F1 — MCP server process has no Django DB-connection hygiene: the June-9 incident class survives in the third process  (LATENT / correctness / verified-by-trace)

The 2026-06-09 outage mechanism — a long-lived process with no request cycle whose
Django connection dies and is reused-closed forever — was fixed **only in the worker**
(custom task decorator, `config/procrastinate.py`). The MCP server is the same kind of
process and got nothing.

Chain:
- The MCP server runs Django: `mcp_server/__main__.py` calls `django.setup()` before
  importing the server.
- Every tenant-scoped tool call resolves context through the Django ORM:
  `mcp_server/context.py:56-66` (`TenantSchema.objects.filter(...).afirst()`, `ts.atouch()`),
  `context.py:100-125` (`Workspace.objects.aget`, `WorkspaceViewSchema.objects.aget`, `vs.atouch()`);
  `server.py` additionally reads/writes `Thread`, `ThreadJob`, `MaterializationRun`
  (e.g. `server.py:563-633`) and defers procrastinate jobs through the Django connector.
- Django's async ORM routes all of this through one thread-sensitive executor thread →
  one `connections["default"]` for the life of the process.
- Hygiene absent: `grep close_old_connections|CONN_MAX_AGE|CONN_HEALTH_CHECKS mcp_server/ config/`
  → only `config/procrastinate.py` (worker) hits. `CONN_MAX_AGE` defaults to 0 but the
  "close at end of request" never fires because FastMCP/Starlette emits no Django
  request signals; `CONN_HEALTH_CHECKS` is unset (False).
- Consequence: after the next RDS restart/backup-window drop/idle TCP reset, **every MCP
  tool call that touches the platform DB fails with `OperationalError: the connection is
  closed` until the MCP container is restarted** — `list_tables`, `describe_table`,
  `query` (context resolution), `run_materialization`, `get_materialization_status`, all
  of it. The agent goes dark for every workspace at once. This is precisely the failure
  signature of the 22-hour worker incident, on the process that was not patched.

Reachable via: every agent chat turn that calls any MCP tool. Complexity: accidental —
the fix exists 30 lines away in `config/procrastinate.py`; the MCP server needs the same
before/after (or a `CONN_HEALTH_CHECKS=True` + periodic close, or a per-tool-call
middleware in `tool_context`).

Note: the in-code postmortem (`config/procrastinate.py:40-44`, `tests/test_worker_db_resilience.py:1-13`)
describes the failure class as a *worker* problem; the docstring's scoping is itself part
of why the sibling site was missed.

### F2 — Worker `asyncio.to_thread` threads: dead-connection hygiene applied at one of three ORM-bearing call sites  (LATENT / correctness / verified-by-trace)

The task decorator's cleanup runs via `sync_to_async(..., thread_sensitive=True)`
(`config/procrastinate.py:31-32`), so it only reaches the **async-ORM executor thread**.
Sync ORM executed on `asyncio.to_thread` pool threads uses *separate per-thread
connections* the decorator cannot touch. The codebase knows this — the comment at
`apps/workspaces/tasks.py:479-483` says exactly that and calls `close_old_connections()`
at the top of `_run_pipeline_with_progress`. But that guard exists at only one of the
three call sites that run Django ORM on a to_thread thread:

| Call site | ORM executed on the pool thread | Guarded? |
|---|---|---|
| `tasks.py:277` → `_run_pipeline_with_progress` → `run_pipeline` | `SchemaManager().provision()` (`schema_manager.py:57-129`), `MaterializationRun.objects.create` (`materializer.py:186`), per-page `.update()` | yes (`tasks.py:483`) |
| `tasks.py:173` (refresh path) → `await asyncio.to_thread(run_pipeline, ...)` directly | same `run_pipeline` ORM as above | **no** |
| `tasks.py:324` and `tasks.py:571` → `SchemaManager().build_view_schema` | `TenantSchema.objects.filter` (`schema_manager.py:263-266`), `Tenant.objects.get` (`:306`), `WorkspaceViewSchema.get_or_create/save` (`:280-287`, `:428-438`) | **no** |

Mechanism: `asyncio.to_thread` uses the loop's default `ThreadPoolExecutor`; threads are
reused across tasks. A thread that ran a pipeline yesterday holds a Django connection
that has since died (the same RDS-event class as F1/F2's parent incident). The next
refresh or view-schema build scheduled onto that thread fails immediately.

Aggravation in the view-schema case: when DDL or anything else raises inside
`build_view_schema`, the except path persists the failure with
`vs.save(update_fields=["state", "last_error"])` (`schema_manager.py:428-430`) — **on the
same dead connection** — so the save raises too, and the caller
(`rebuild_workspace_view_schema`, `tasks.py:572-577`) explicitly assumes "build_view_schema
already saves state=FAILED before re-raising" and writes nothing. The
`WorkspaceViewSchema` row is left **stuck in PROVISIONING**, which `load_workspace_context`
(`mcp_server/context.py:115-123`) treats as "no active view schema" — multi-tenant
workspace unqueryable with no FAILED state or `last_error` for the status API to surface.

Reachable via: `POST /api/workspaces/<id>/refresh/` (`api/views.py:365` defers
`refresh_tenant_schema`); sibling/regular view-schema rebuilds dispatched by
`materialize_workspace` and `add/remove_workspace_tenant`. Complexity: accidental —
"fixed-where-it-bit": the hygiene was added where the 22h incident manifested
(`_run_pipeline_with_progress`) and not at the sibling thread-entry points. The honest
fix is a tiny `_to_thread_with_fresh_connections()` helper (or putting
`close_old_connections()` at the top of `run_pipeline` and `build_view_schema` themselves).

Also noted (minor, same mechanism): hygiene runs only at thread *entry*, so each pool
thread's connection idles open between pipeline runs; with the default executor cap
(`min(32, cpu+4)` threads) a worker can strand a few dozen idle platform-DB connections.

### F3 — Checkpointer pool singleton: unsynchronized init race, and `force_new` closes the pool out from under concurrent streams  (LATENT / correctness / verified-by-trace for the code paths; impact strong-inference)

`apps/chat/checkpointer.py:18-57` lazily builds a module-global `AsyncConnectionPool`
(max_size=20) + `AsyncPostgresSaver` with **no lock**:

1. **Cold-start race.** Two concurrent first requests both see `_checkpointer is None`.
   B then sees `_pool is not None` (A's half-open pool) and `await _pool.close()`s it
   (line 27) *while A is opening/using it*; A's `setup()` then fails on the closed pool
   and A 500s or retries. With uvicorn `--workers 4` this is per-process and settles
   eventually, but cold deploys under load can produce a burst of failed first chats.
2. **`force_new` collateral.** `chat_view`'s retry path (`apps/chat/views.py:174-185`)
   calls `ensure_checkpointer(force_new=True)` on **any** exception from
   `build_agent_graph` — not just connection errors. That closes the shared pool that
   every other in-flight chat stream in the process is using for checkpoint writes
   (`langgraph_to_ui_stream` → saver.aput on each superstep). One request hitting a
   transient build error degrades every concurrent conversation in that worker process;
   their checkpoint writes fail mid-turn and those turns are lost.
3. **No health-check on borrow.** The pool is built without `check=`
   (`checkpointer.py:29-37`), and psycopg_pool does not validate connections on
   `getconn()` by default — after a DB blip, up to pool-size checkpoint operations fail
   before broken connections are discarded. The only recovery mechanism is the blunt
   `force_new` above. (`max_lifetime` default ≈1h does bound staleness.)

Reachable via: `POST /api/chat/` (every chat turn); worker resume task shares the same
module (`tasks.py:859`). Complexity: accidental — an `asyncio.Lock` around init, a
narrower exception filter for `force_new`, and `check=AsyncConnectionPool.check_connection`
are all small changes.

### F4 — Chat stream's 300s agent timeout is only checked between events; a stalled agent hangs the stream indefinitely  (LATENT / correctness / verified-by-trace)

`apps/chat/stream.py:100-107`:

```python
deadline = asyncio.get_event_loop().time() + AGENT_TIMEOUT_SECONDS
while True:
    if asyncio.get_event_loop().time() > deadline:
        raise TimeoutError(...)
    event = await event_stream.__anext__()   # ← no timeout on the await
```

The deadline is evaluated *before* awaiting the next event; the await itself is
unbounded. If the LLM call or an MCP tool call stalls without emitting events, the
TimeoutError branch is unreachable and the SSE response hangs until the client
disconnects or some downstream library timeout fires (anthropic SDK default is 10
minutes; the MCP streamable-http client's read behavior was not verified). Contrast with
the worker resume path, which correctly bounds the same operation with
`asyncio.wait_for(agent.ainvoke(...), timeout=...)` (`tasks.py:1161-1164`). The fix is
`asyncio.wait_for(event_stream.__anext__(), timeout=remaining)`. Also: on the timeout
path the generator is abandoned without `aclose()`, so the underlying agent task can
keep running (and keep writing checkpoints) after the user has been told it timed out.

Reachable via: every `POST /api/chat/` turn. Complexity: accidental.

### F5 — Worker death mid-job still strands ThreadJobs forever: 'doing' is treated as alive and nothing rescues stalled procrastinate jobs  (LATENT / correctness / verified-by-trace for Scout code; procrastinate semantics strong-inference)

`reconcile_stale_thread_job` (`tasks.py:748-749`) returns `None` whenever the
procrastinate job status is `todo` or `doing` — "still active, don't touch". But a job
whose worker died uncleanly (OOM, hard kill, deploy timeout) stays `doing` in
`procrastinate_jobs` forever. Procrastinate 3.8.1 ships the machinery to detect this
(`manager.get_stalled_jobs` / heartbeats, `prune_stalled_workers` —
`.venv/.../procrastinate/manager.py:223,973`) but it is opt-in and **nothing in Scout
calls it** (grep for `stalled|heartbeat` over `apps/ config/ mcp_server/`: no hits).
So:

- worker dies mid-`materialize_workspace` → procrastinate row stuck `doing` →
- both the worker janitor (`expire_stale_thread_jobs`) and the API-side backstop
  (`api/jobs_views.py:117-135`) call the same `reconcile_stale_thread_job`, get `doing`,
  and skip the row **every tick, forever** →
- ThreadJob stays PENDING, frontend spinner persists, no resume, no failure card.

This is the 2026-05-30 "zombie doing jobs" incident, still unfixed (the project memory
note says as much; this trace confirms the code on HEAD). The June-10 connection-hygiene
work fixed the *connection death* trigger, not the *process death* trigger.

Reachable via: any uncontrolled worker termination while a job runs — routine deploys
that exceed the stop grace period count. Complexity: essential difficulty (crash-safe
job rescue is genuinely hard) but the unused upstream stalled-job API makes the fix
mostly wiring: a periodic task calling `get_stalled_jobs`/retry, or treating
`doing`-for-much-longer-than-any-legitimate-run as dead in the janitor.

### F6 — RecipeRunner passes a kwarg `build_agent_graph` no longer accepts: recipe execution is broken-now and strands runs in RUNNING  (BROKEN-NOW / correctness / verified-by-trace)

Chain:
- `POST /api/workspaces/<id>/recipes/<id>/run` → `RecipeRunView.post`
  (`apps/recipes/api/views.py:89,105-108`) → `RecipeRunner(...).execute()`.
- `execute()` (`services/runner.py:185-191`) creates the `RecipeRun` row
  (status=RUNNING, line 189) and then calls `async_to_sync(self._build_graph)()` —
  **outside the try block**.
- `_build_graph` (`runner.py:115-119`) calls
  `build_agent_graph(tenant_membership=self._tenant_membership, user=self.user, checkpointer=None)`.
- `build_agent_graph`'s actual signature (`apps/agents/graph/base.py:480-486`) is
  `(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)` — no
  `tenant_membership`, no `**kwargs` → `TypeError` on every call.
- The TypeError propagates out of `execute()` → DRF 500; the `RecipeRun` is left in
  RUNNING forever (no except path runs).

Even if the kwarg were fixed, `execute()` then calls the **sync** `graph.invoke()`
(line 226) on a graph whose MCP/artifact tools are async-only, and builds
`initial_state` from `workspace.external_tenant_id` (line 217), an attribute the current
`Workspace` model does not obviously have — this path has drifted at several layers,
which says it has not been exercised since the graph signature changed.
`execute_async()` has no production caller (vestige). This replicates the v1 "recipes ↔
graph signature" finding with the exact mechanism. Complexity: accidental (consumer not
migrated during a signature change).

### F7 — `apps/agents/memory/checkpointer.py` is dead code with a dangerous pattern inside  (DEBT / velocity / verified-by-trace)

`get_postgres_checkpointer` / `get_sync_checkpointer` have no callers outside their own
module and `memory/__init__.py` re-exports (grep). The live implementation is
`apps/chat/checkpointer.py`. The dead one swallows *all* exceptions and silently yields
`MemorySaver` in production (`memory/checkpointer.py:138-145`) — "conversations will NOT
be persisted" as a log line — exactly the silent-fallback class the live module was
written to avoid (it raises in prod, `chat/checkpointer.py:49-55`). Risk is that a
future caller reaches for the well-docstringed dead one. Delete it.

### F8 — Connection-per-operation costs across the seams  (DEBT / cost-perf / verified-by-trace for the code, impact strong-inference)

- Every MCP `query`/metadata call opens a fresh TLS psycopg connection to the managed DB
  (`mcp_server/services/query.py:41,73`); every artifact `query-data` request does the
  same from the API process (`artifacts/views.py:822` → same service). Each agent turn
  can mean several handshakes.
- Every chat POST constructs a fresh `MultiServerMCPClient` and re-fetches the tool list
  over HTTP (`apps/agents/mcp_client.py:31-57`).
- API runs uvicorn `--workers 4` (`config/deploy.yml:17`); each worker can grow a
  checkpointer pool to 20 → up to 80 platform-DB connections from chat alone, plus
  per-request Django connections (`CONN_MAX_AGE=0` → connect/disconnect per request).
  No PgBouncer is visible in the deploy configs.

None of this is wrong today; it is headroom that will be spent invisibly as usage grows.

### F9 — Single-slot worker: one long materialization delays every queued task including the janitors  (DEBT / cost-perf / strong-inference)

The worker command is bare `python manage.py procrastinate worker`
(`config/deploy-worker.yml:16`, `Procfile.dev`); procrastinate's default concurrency
is 1. A multi-hour materialization (large Connect opportunity) occupies the only slot;
`expire_inactive_schemas`, `expire_stale_thread_jobs`, sibling view-schema rebuilds, and
queued resumes all wait behind it. The API-side reconcile backstop
(`api/jobs_views.py:117-135`) covers the ThreadJob-spinner symptom but not TTL
enforcement or rebuilds. The resume docstring (`tasks.py:368-370`) even relies on
"workers handle one task at a time per slot" for a timing argument — concurrency
assumptions are load-bearing and undocumented in the deploy config.

### F10 — Comment/logic mismatch in chat rate limiter; per-process cache in a 4-worker deployment  (COSMETIC / correctness / verified-by-trace)

`apps/chat/rate_limiting.py:28-34` claims `check_and_record` "performs a single cache
read/write cycle to avoid TOCTOU races" — it is a non-atomic `aget` → filter → append →
`aset` (lines 43-53); concurrent requests can all pass. And the backing cache is
`LocMemCache` with an explicit base-settings note "set up a shared cache for production"
(`config/settings/base.py:318-325`) that production.py never acts on — so with
`--workers 4` the effective chat limit is ~4× the configured one and the
tenant-refresh TTL (`users/views.py:91-121`) re-fires per worker process. Low stakes
today; the comment should stop claiming atomicity.

### F11 — Acknowledged create-order race at the MCP→worker seam (recorded for completeness)  (DEBT / correctness / verified-by-trace, acknowledged in-code)

`run_materialization` defers the job before creating the ThreadJob
(`mcp_server/server.py:606-635`, atomicity note in-code); the worker side hedges with a
~3.75s sleep-poll for the row (`tasks.py:363-396`) and a TODO admitting the clean fix
(pre-create the ThreadJob; nullable `procrastinate_job_id` migration). Janitor catches
the miss eventually. Known, bounded, and honestly documented — flagged here only because
it is a cross-process ordering contract held together by sleeps.

---

## What's actually fine (verified)

- **The worker task decorator is correct for what it covers.** Cleanup runs via
  `sync_to_async(thread_sensitive=True)`, which in a plain-asyncio worker is the same
  single executor thread Django's async ORM uses — so `close_old_connections` really
  does reach the connections the tasks use (`config/procrastinate.py:27-32`), and
  `tests/test_worker_db_resilience.py` both reproduces the dead-connection failure and
  pins that every task in `apps.workspaces.tasks` registers through the wrapper.
- **Async ORM discipline is excellent.** AST scan of all 142 async functions: zero sync
  ORM calls. Lazy-FK hot spots are pre-fetched (`tasks.py:1026`, `:828-831`,
  `jobs_views.py:103`); `aresolve_credential` does `select_related("account", "app")`
  before `refresh_oauth_token` touches `token.app` (`credential_resolver.py:87-91`) —
  the 8104ce1 SynchronousOnlyOperation class is fixed at both task call sites, and the
  sync `resolve_credential` no longer exists to be misused.
- **`sync_to_async` policy is honored.** The only production uses are the sanctioned
  transactional-write blocks (`users/views.py:47,281`) and the decorator's cleanup.
  `async_to_sync` uses (DRF sync views: `workspaces/api/views.py:275,454`,
  `auth_views.py:240`, allauth `signals.py:66-76`) all execute in thread-sensitive sync
  contexts where asgiref's CurrentThreadExecutor makes the nesting safe; token objects
  are select_related before crossing (`auth_views.py:230-232`).
- **psycopg-direct acquire/release is disciplined everywhere**: context managers or
  try/finally close in `schema_manager.py` (all sync + async paths),
  `materializer._load_and_commit_source` (`materializer.py:699-729`, including
  rollback-then-close on error), `mcp_server/services/query.py`, `metadata.py`.
- **The resume task's claim/terminal CAS design** (`tasks.py:1043-1050, 1264-1288`) and
  `_procrastinate_job_status`'s None-means-don't-touch contract (`tasks.py:693-725`) are
  careful, well-commented concurrency code — the 19-commit fix chain converged on
  something solid (modulo F5, which is about process death, not these transitions).
- **MCP server import order** dodges the `current_app`-is-a-FutureApp trap that bit the
  worker: `mcp_server/__main__.py` runs `django.setup()` before `server.py` imports
  `current_app`, so its rollback `cancel_job_by_id_async` (`server.py:633`) gets the
  real App.
- **API-process connection lifecycle** is standard Django-per-request and healthy;
  the API-side reconcile backstop (`jobs_views.py`) genuinely de-singles the janitor.

## Coverage log

**Deep-read (line-by-line):** `config/procrastinate.py`; `apps/workspaces/tasks.py`;
`apps/chat/checkpointer.py`, `views.py`, `helpers.py`, `stream.py`, `rate_limiting.py`,
`thread_views.py` (lines 1–190); `apps/agents/memory/checkpointer.py`, `mcp_client.py`;
`apps/workspaces/services/schema_manager.py`, `services/workspace_service.py`,
`workspace_resolver.py`, `api/jobs_views.py`; `mcp_server/context.py`,
`services/query.py`, `__main__.py`; `apps/users/views.py`, `decorators.py`,
`services/credential_resolver.py`, `services/token_refresh.py`;
`apps/recipes/services/runner.py`; `config/asgi.py`; settings DATABASES/CACHES sections.

**Skimmed (targeted sections/greps only):** `mcp_server/server.py` (~300 of 982 lines:
run_materialization, setup/main); `mcp_server/services/materializer.py` (lines 96–295,
600–760, 1060–1110 of 2,030); `apps/agents/graph/base.py` (signature, agent_node, grep
of ORM/function map); `apps/agents/tools/*` (grep-level: confirmed async ORM only);
`apps/users/auth_views.py` (providers_view region), `signals.py`,
`services/tenant_resolution.py` (grep-level); `apps/artifacts/views.py` (QueryData/Data
views only); `apps/workspaces/api/views.py` (structure + refresh path only);
`apps/transformations/views.py` (trigger action), `services/executor.py` (imports/dirs),
`mcp_server/services/dbt_runner.py`, `metadata.py` (grep-level);
`tests/test_worker_db_resilience.py` (header + first tests); `Procfile.dev`,
`config/deploy.yml`/`deploy-worker.yml` (cmd lines); procrastinate 3.8.1 package (stalled
APIs only).

**Not examined:** all of `frontend/`; `apps/users/services/merge.py`, `ocs_team.py`,
`api_key_providers/`, `adapters.py`, allauth provider code; `mcp_server/loaders/*` (19
files — HTTP client behavior inside the to_thread context not audited),
`sql_validator.py`, `envelope.py`, `pipeline_registry.py`, materializer table writers
(lines 1110–2030); `apps/artifacts/services/export.py`, sandbox rendering;
`apps/knowledge/` entirely; `apps/recipes/api/views.py` beyond the run action and
models; `apps/transformations/` models/lineage/staging; `apps/chat/message_converter.py`,
`models.py` details; `apps/workspaces/api/{workspace_views,materialization_views,
jobs_cancel}.py`, `permissions.py`, management commands; `config/urls.py`, middleware
list; all tests except the resilience test; Langfuse `tracing.py` (whether its HTTP
flushes block the loop was not checked); the MCP streamable-http client's
timeout/reconnect behavior (relevant to F4's blast radius); whether procrastinate's
Django connector itself can wedge on a dead connection in the *defer* direction from the
API/MCP processes.
