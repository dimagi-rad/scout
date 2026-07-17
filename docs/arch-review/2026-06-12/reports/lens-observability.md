# Lens Review: Observability

*Reviewer: cross-cutting lens — "could we debug the next production incident from what is
actually emitted?" Scope: logging config, audit logging, tracing (Langfuse), error
monitoring (Sentry), task tracking (TaskBadger), job/run records, error surfacing to
operators and users, alerting infrastructure. Date: 2026-06-12. Report only — no code
changed.*

## The observability stack as-built

| Layer | Mechanism | Coverage |
|---|---|---|
| Logs | Python `logging` → console → Docker `awslogs` driver → CloudWatch (`/scout/{api,worker,mcp,frontend}`, 30-day retention, `infra/scout-stack.yml:360-388`) | all 4 services |
| Errors | `sentry_sdk.init` in `config/settings/base.py:296-303` (added 2026-04-21, `b789524`) — runs in any process that loads Django settings, i.e. API, worker, **and** MCP server (`mcp_server/server.py:_setup_django`). Frontend: `@sentry/react` in `frontend/src/main.tsx:8-10` | all processes, when `SENTRY_DSN` set (it is, in all three deploy ymls) |
| Tasks | TaskBadger Procrastinate integration, `config/taskbadger.py` (added 2026-05-27, `318881f`), auto-tracks every `@app.task` | worker tasks |
| LLM tracing | Langfuse v3 via `apps/agents/tracing.py` | **chat turns only** (see F5) |
| Job/run records | `MaterializationRun.result/progress`, `ThreadJob.error_summary`, `WorkspaceViewSchema.last_error` | good where populated (see F6) |
| Metrics | none — no prometheus/statsd/datadog anywhere (`grep` over `apps/ mcp_server/ config/`) | — |
| Alerts | none in repo — zero `AWS::CloudWatch::Alarm` / SNS resources in `infra/scout-stack.yml`; `/health/` returns a static 200 (`apps/workspaces/views.py:8-13`) | — |

The recurring shape of the past incidents is **not** "nothing was logged" — it is
(a) signals emitted at levels that nothing watches, (b) audit/forensic trails that
are partially or wholly silent, and (c) absence-of-activity failures (dead worker)
that no emitted-signal system can see by construction.

---

## Findings

### F1. The Django-side agent audit log is silently dropped in production, and its one attribution field is rename residue (BROKEN-NOW / correctness, verified-by-trace)

The only place the platform records "this user, in this thread, caused this agent tool
call" is the `scout.agent.audit` logger in the chat stream translator:

- `apps/chat/stream.py:76` — `audit_logger = logging.getLogger("scout.agent.audit")`
- `apps/chat/stream.py:182-188` — on every `on_tool_end`:
  ```python
  audit_logger.info(
      "tool_call tool=%s user_id=%s thread_id=%s project_id=%s",
      tool_name,
      input_state.get("user_id", ""),
      config.get("configurable", {}).get("thread_id", ""),
      input_state.get("project_id", ""),
  )
  ```

Chain to the consequence:

1. Entry: every production chat turn → `apps/chat/views.py:chat_view` →
   `langgraph_to_ui_stream` (`apps/chat/views.py:243-247`).
2. `audit_logger.info(...)` fires at INFO (`stream.py:182`).
3. Production logging config (`config/settings/production.py:37-73`) configures only
   `django`, `apps`, and `mcp_server` loggers; root is `"level": "WARNING"`
   (`production.py:52-55`). `scout.agent.audit` matches none of the named loggers, so
   it inherits the root effective level WARNING → **the INFO audit line is suppressed
   entirely in production**. (Dev root is INFO — `development.py:46-49` — so the line
   appears in development, hiding the gap.)
4. Independently, even where it does emit: `input_state` is built in
   `apps/chat/views.py` with keys `messages, workspace_id, user_id, user_role,
   thread_id` — there is no `project_id` key. `project_id=` is **always empty**:
   residue of the 2026-03-17 `projects → workspaces` rename. The line also logs no
   tool input (the SSE chunk also sends `"input": {}`, `stream.py:196`).

Reachable via: every chat message in production. Impact: the audit trail TODO.md
treats as "done" at the logger level ("Audit logging to `mcp_server.audit` logger" —
checked) has a Django-side twin that emits nothing in the environment where it
matters, and would lack workspace attribution if it did. Complexity: accidental.

### F2. The MCP audit trail cannot answer "who" — and the destructive `teardown_schema` tool is the worst case (DEBT / security, verified-by-trace)

The MCP server has real structured audit logging — `tool_context` in
`mcp_server/envelope.py:90-116` wraps all 11 tools (verified: every `@mcp.tool` in
`mcp_server/server.py` opens `tool_context`, lines 117, 187, 241, 302, 344, 385, 417,
456, 545, 662, 818) and the `query` tool does include the SQL
(`server.py:344`: `tool_context("query", workspace_id, sql=sql)`).

But:

- `context_id` is the **workspace id only**. No user, no thread. The only tool that
  receives `user_id` is `run_materialization` (`server.py:521-526`), and even there
  the audit line doesn't include it (extra fields are whatever the call site passes;
  `server.py:545` passes none).
- `teardown_schema` (`server.py:802-866`) drops the view schema **and every tenant
  schema in the workspace** (`DROP SCHEMA ... CASCADE` via `ateardown`) and its audit
  record is `tool_call tool=teardown_schema context_id=<ws> status=success confirm=True`
  — no actor, and no `logger.*` call anywhere in the tool body listing what was
  dropped beyond the returned envelope. This tool is agent-reachable (it is in
  `MCP_TOOL_NAMES` wiring in `apps/agents/graph/base.py`), i.e. an LLM can trigger
  irreversible multi-schema destruction whose only trail is a console line in
  CloudWatch with no user attribution.
- The trail is logger-only with 30-day CloudWatch retention; `TODO.md:40`
  acknowledges this: *"[ ] Append-only audit DB table — `MCPAuditLog` Django model
  (user ID, tenant ID, tool, args redacted, status, timing); replace logger-only
  audit trail"* — unchecked.
- Side note: the `query` audit line embeds raw SQL (literals may contain PII) into
  CloudWatch; the scrub list is exactly `{"oauth_tokens"}` (`envelope.py:82`).

Reachable via: every agent MCP tool call. Complexity: accidental (the user identity
is available one hop earlier in the graph injection node and is already threaded for
`run_materialization`).

### F3. Schema destruction is silent on success — the TTL janitor emits zero log lines (DEBT / velocity+correctness, verified-by-trace)

The exact forensic question of the 2026-06-10 incident ("why did this freshly
materialized schema disappear?") is still unanswerable from logs:

- `expire_inactive_schemas` (`apps/workspaces/tasks.py:516-558`, cron `*/30`):
  flips `TenantSchema` and `WorkspaceViewSchema` rows ACTIVE→TEARDOWN and defers
  drop tasks. **The function body contains no logging at all** — not the schema id,
  not the `last_accessed_at` value that justified expiry, not the cutoff, not a
  count. (Verified by reading the full body; the only logger calls in that region of
  tasks.py belong to neighboring functions.)
- `teardown_schema` (`tasks.py:608+`) and `teardown_view_schema_task`
  (`tasks.py:585-606`): log **only failures** (`logger.exception`); a successful
  `DROP SCHEMA ... CASCADE` of a data-bearing schema produces no log line.
- `_fail_dependent_view_schemas` (`tasks.py:666-690`) returns the number of sibling
  workspaces it just broke; the caller (`teardown_schema`) discards the return value
  — so cascading damage to *other* workspaces is unlogged too.

Post-incident, you can partially reconstruct from `procrastinate_jobs` rows (task
args) and row states, but `last_accessed_at` is overwritten by later touches, so the
decision input is unrecoverable. Had PRs #227-#232 not found the resurrect-without-touch
bug by code reading, the logs could not have shown janitor-dropped-fresh-schema.
Contrast: the MCP cancel path *does* log (`server.py:486`), and the ThreadJob janitor
logs every reconcile action (`tasks.py:767-792`) — the convention exists; the TTL/
teardown family just never adopted it. Complexity: accidental.

### F4. There is no detection layer: zero alarms, a static health check, and worker death remains observable only as silence (DEBT / velocity, strong-inference)

- `infra/scout-stack.yml` contains CloudWatch **log groups** (lines 360-388) but no
  `AWS::CloudWatch::Alarm`, no SNS topic, no metric filters. Grep for
  `Alarm|SNS|Topic` over `infra/` returns nothing.
- `/health/` (`apps/workspaces/views.py:8-13`) returns `{"status": "ok"}`
  unconditionally — no DB ping, no queue check. It is wired to Docker/LB health only
  for the API container; the worker and MCP have no health surface at all.
- The worker has no heartbeat. TaskBadger (added 2026-05-27) auto-tracks task
  *executions* — when the worker is dead, the failure signature is the **absence**
  of TaskBadger events and the absence of janitor log lines, which nothing in this
  repo turns into a page. (Whether TaskBadger's server-side monitoring is configured
  to alert on missing scheduled runs is not verifiable from the repo — flagging as
  the load-bearing unknown.)
- Calibration point: Sentry was live from 2026-04-21 (`b789524`), well before the
  2026-06-09 incident, and the dying tasks raised `psycopg.OperationalError` through
  `logger.exception` paths that Sentry's logging integration converts to events —
  yet the outage ran ~22h and was discovered via stuck UI. Emitted-but-unrouted
  signals are this codebase's demonstrated failure mode; adding more emission without
  an alerting layer will not change the next incident's detection time.
- The post-incident fixes are resilience (connection-hygiene decorator,
  `config/procrastinate.py:35-77`, explicitly TEMPORARY) and an API-side reconcile
  backstop (`apps/workspaces/api/jobs_views.py:117-134`) — both reduce blast radius;
  neither detects. A worker that dies for any *new* reason (OOM loop, bad deploy,
  queue table lock) is still found by users first.

Status LATENT-leaning-DEBT: nothing is broken today, but the 22h-detection failure
class is structurally unaddressed. Complexity: essential work not yet done (some
alerting layer is required for a multi-process system with cron-driven correctness).

### F5. Langfuse traces only the interactive chat turn — resume turns and recipe runs are dark (DEBT / velocity, strong-inference)

- Chat: `apps/chat/views.py:218-247` attaches `get_langfuse_callback()` to
  `config["callbacks"]` and wraps the stream in `langfuse_trace_context` — fully
  traced, with session/user attribution. Good.
- Resume-after-materialization (`apps/workspaces/tasks.py:1155-1165`): wraps
  `agent.ainvoke` in `_resume_langfuse_span` (`tasks.py:870-892`) — a bare
  `start_as_current_observation` span whose input is `{thread_job_id, thread_id,
  status}`. **No `CallbackHandler` is attached to the config**, so the LLM
  generations, tool calls, token counts and prompts inside the resume turn are not
  captured; the span also never records output. The resume turn is precisely the
  turn that runs unattended in the worker — the one you most need traces for when
  "the agent said something wrong after materialization".
- Recipe runs (`apps/recipes/services/runner.py:312` `graph.ainvoke(initial_state,
  config=config)`): no Langfuse reference anywhere in the file (grep). Recipe
  executions — including scheduled/replayed analyses — produce zero traces, and use
  thread ids of the form `recipe-run-<id>` so they would not stitch into any chat
  session anyway.

Confidence is strong-inference rather than verified-by-trace only because it rests on
Langfuse v3 SDK semantics (LangChain instrumentation requires the CallbackHandler in
`config["callbacks"]`) which I did not re-verify against the SDK source. Complexity:
accidental — the helper exists and is two lines to attach.

### F6. Cascade-FAILED view schemas report a fabricated cause: "View schema build failed." (LATENT / correctness, verified-by-trace)

`WorkspaceViewSchema.last_error` is documented as "Most recent build_view_schema
failure message; cleared on a successful build" (`apps/workspaces/models.py:233-237`).
Post-incident PR #229 made `get_schema_status` surface it. But there is now a writer
that flips state without writing the reason:

1. `teardown_schema` drops a shared tenant schema → cascade-drops sibling
   workspaces' namespaced views → `_fail_dependent_view_schemas`
   (`apps/workspaces/tasks.py:685-689`) does
   `.aupdate(state=SchemaState.FAILED)` — **state only, `last_error` untouched**.
2. Agent/MCP consumers then read `get_schema_status`
   (`mcp_server/server.py:751-763`): `"error": failed_vs.last_error or "View schema
   build failed."` — so the agent (and the status API, and the operator) is told
   either a *stale previous build error* or the fallback "View schema build failed.",
   when the actual cause is "tenant `X`'s schema was torn down (TTL or manual)".
3. The resume prompt then instructs the agent to tell the user "a system-side fix is
   required" and "do NOT re-run materialization" (`tasks.py:1090-1095`) — advice that
   is wrong for this cause: re-materializing the torn-down tenant is exactly the fix.

This is a fresh sibling of the incident-1d class ("failures swallowed / wrong story
told to the agent"), introduced by the fix wave itself. Reachable via: TTL expiry of
any tenant schema shared into a multi-tenant workspace (cron `*/30`), or the
agent-invocable `teardown_schema` tool. Complexity: accidental.

### F7. Artifact live queries execute stored SQL with no audit record (DEBT / velocity+security, verified-by-trace)

`apps/artifacts/views.py:822` calls `mcp_server.services.query.execute_query`
directly in the **API process** — outside the MCP `tool_context` wrapper, so the
`mcp_server.audit` line is never emitted; `execute_query` itself logs only validation
failures (WARNING) and execution errors (ERROR) (`mcp_server/services/query.py:113,
132`), never successful executions. The F1 Django-side audit logger doesn't cover
this path either (it is not an agent tool call). Net: every artifact view/refresh
runs N stored SQL statements against tenant data with no record of who/when/what.
Same gap class as F2, on a different entry point. Complexity: accidental.

### F8. User-facing degradations are logged at WARNING, which nothing watches (DEBT / velocity, strong-inference)

Sentry's logging integration defaults to event_level=ERROR; production console shows
WARNING+ but no one greps CloudWatch proactively (no metric filters — F4). The
following *user-visible* failures therefore produce no Sentry event and no alert:

- OAuth tenant-resolution failures (`apps/users/signals.py:61,68,73,78`) — "user logs
  in and sees no workspaces", a recurring support class, WARNING.
- Thread history load failure (`apps/chat/thread_views.py:101`) — user sees an empty
  thread, WARNING.
- Agent panic-loop escalation (`apps/agents/graph/base.py:608-613`) — the #190
  circuit breaker fires, turn aborted, WARNING; no counter/metric, so a prompt/
  validator-drift regression (a known recurring class) reappearing would be invisible
  until users complain.
- Chat stream timeout (`apps/chat/stream.py:213`) — WARNING.
- MCP tool-load circuit breaker open (`apps/agents/mcp_client.py:63` is ERROR on
  failure — fine — but recovery/cooldown state at INFO only).

Individually defensible; collectively the level convention encodes "warning = user
already suffered, nobody is told". Complexity: mixed — choosing levels is essential
work; the absence of any consumer for WARNING is the accidental part.

### F9. Error-correlation is inconsistent: one endpoint mints user-visible error refs, the rest don't, and nothing correlates across processes (COSMETIC / velocity, verified-by-trace)

`chat_view`'s agent-build failure path creates a hashed `error_ref`, logs it, and
returns it to the user (`apps/chat/views.py:186-191`) — a good pattern for support
("Ref: a1b2c3d4"). But the streaming error path one screen later
(`apps/chat/stream.py:226-239`) returns a bare "An error occurred while processing
your request." with no ref, and no other surface uses refs. There is no request-id
middleware, and nothing propagates a correlation id across API → MCP → worker:
reconstructing one user action across the three CloudWatch log groups relies on
workspace_id + timestamps. The MCP envelope carries `timing_ms` but no trace/req id
(`mcp_server/envelope.py:34-54`). Complexity: accidental.

---

## What the past incidents say about these gaps

| Incident | Could today's emission debug it? |
|---|---|
| 06-10 TTL janitor drops fresh schema | **No** — janitor decision inputs unlogged (F3); drop success unlogged (F3); cascade damage count discarded (F3); misattributed `last_error` on siblings is new (F6) |
| 06-10 view-schema failures swallowed | Mostly fixed (#229: `last_error` + status API + resume prompt) — except the F6 writer that bypasses `last_error` |
| 06-09 worker dead 22h | Resilience fixed; **detection unfixed** (F4) — a novel worker-death cause repeats the 22h pattern |
| 05-30 zombie `doing` jobs | Janitor reconcile now logs actions (good); queue-depth/stalled-job visibility still absent (F4) |
| "who ran this destructive thing?" (future) | **No** — no actor in MCP audit (F2), Django-side audit dead in prod (F1), artifact SQL unaudited (F7) |

## What's fine

- **MCP tool audit envelope mechanics** (`tool_context`) — uniform, structured,
  includes SQL + timing + status, wraps all 11 tools, and the `mcp_server.audit`
  logger *is* routed in production (child of the configured `mcp_server` logger).
- **The resume task's instrumentation** — Sentry breadcrumbs at every phase
  transition, start/complete logs with elapsed times, synthetic user-facing failure
  messages, terminal-state discipline (`tasks.py:1128-1260`). This is the standard
  the rest of the codebase should be held to.
- **MaterializationRun forensic record** — per-source state/rows/error/attempts/
  `failed_at`/cursor watermark in `run.result` (`materializer.py:328-470`); pre-loop
  failures stamped terminal with error (`materializer.py:405-430`); failure summaries
  composed into `ThreadJob.error_summary` for the UI (`tasks.py:65-122`).
- **Production checkpointer fails loud** — `apps/chat/checkpointer.py:43-55` raises
  in production and logs at ERROR (Sentry event); the silent MemorySaver fallback in
  `apps/agents/memory/checkpointer.py` is effectively dead code (no non-test callers).
- **Sentry wiring breadth** — all three backend processes get the same `base.py`
  init with environment + release tags; frontend has `@sentry/react`; the Connect
  loader even propagates the upstream `sentry-trace` header into tags for cross-org
  correlation (`tasks.py:291-305`, `loaders/connect_base.py:212`) — genuinely good.
- **Log shipping & retention** — all four services use the awslogs driver into
  pre-created log groups with 30-day retention enforced in CloudFormation.
- **TaskBadger** integration is idempotent, env-gated, and auto-tracks every task
  with args recorded.

## Coverage log

**Deep-read** (line-by-line): `config/settings/production.py`,
`config/settings/development.py`, `config/taskbadger.py`, `config/procrastinate.py`,
`apps/agents/tracing.py`, `apps/chat/stream.py`, `apps/chat/checkpointer.py`,
`apps/agents/memory/checkpointer.py`, `mcp_server/envelope.py`,
`mcp_server/services/query.py`, `mcp_server/context.py`, `apps/workspaces/views.py`,
`apps/workspaces/tasks.py` (lines 1-130, 400-1260 — i.e. most of it),
`mcp_server/server.py` (lines 520-560, 651-982 + tool/audit grep map),
`mcp_server/services/materializer.py` (lines 200-500),
`apps/chat/views.py` (lines 180-249), `apps/artifacts/views.py` (lines 780-840),
`apps/recipes/services/runner.py` (lines 260-330),
`apps/workspaces/models.py` (MaterializationRun + WorkspaceViewSchema sections).

**Skimmed** (greps + targeted excerpts): `apps/agents/graph/base.py`,
`apps/agents/mcp_client.py`, `config/deploy.yml`, `config/deploy-worker.yml`,
`config/deploy-mcp.yml`, `infra/scout-stack.yml`, `TODO.md`,
`apps/chat/thread_views.py`, `apps/workspaces/api/jobs_views.py`,
`apps/agents/tools/{artifact,learning,recipe}_tool.py`, `mcp_server/loaders/*`
(logging-call census only), `apps/workspaces/services/schema_manager.py` (logging
census only), `apps/users/{signals,auth_views}.py`, `apps/users/services/merge.py`,
`frontend/src/main.tsx`, `config/settings/base.py` (observability sections only).

**Not examined**: `mcp_server/services/{metadata,sql_validator,dbt_runner}.py`;
`apps/transformations/*` (dbt run logging/observability entirely unassessed);
auth-event audit trail depth (login/logout/merge events — only grepped, no trace of
what allauth itself logs); management commands' output/logging; frontend error
handling beyond Sentry init (error boundaries, toast/error surfacing on API failures,
the 4 `console.error` sites); `apps/knowledge/*`; `apps/artifacts/services/export.py`
and sandbox-rendering error paths; `apps/chat/{helpers,message_converter,
rate_limiting}.py`; `.github/workflows/` (deploy-time observability);
procrastinate's own internal log levels (claim about its failure logs is inferred,
not verified against the library); Langfuse SDK semantics for
`propagate_attributes`/`CallbackHandler` (F5 rests on documented v3 behavior, not
SDK source); TaskBadger server-side monitor/alert configuration (not in repo —
explicitly the open question inside F4); the public share endpoints' access logging
(`public_thread_view`, `/widget.js`) — likely unaudited access but not traced.
