# Scout Cartography — Phase 0 (Architecture Review v2)

*Produced 2026-06-12 by the cartographer agent. This is the shared map for specialist
reviewers (verticals, lenses, seams, journeys). Generalists do NOT receive this file.
This document maps; it does not judge. Nothing here is a finding.*

Repo: `/Users/bderenzi/Code/dimagi/scout`, branch `main`, HEAD `35e4230` (2026-06-11).
History: **787 commits** (680 non-merge), 2026-02 → 2026-06.

---

## 1. Module inventory

### Size overview (LOC, excluding `__pycache__`/migrations unless noted)

| Area | LOC | Notes |
|---|---|---|
| `apps/` (Django) | ~19,000 | 9 apps, see below |
| `tests/` | ~27,000 | test:app ratio ≈ 1.4:1 — tests are the largest body of code |
| `frontend/src/` | ~13,100 | 117 TS/TSX files |
| `mcp_server/` | ~5,850 | standalone FastMCP process |
| `config/` | ~1,135 | settings, urls, ASGI, deploy yml |
| `pipelines/` | 118 | 3 YAML pipeline definitions |
| `infra/` | 476 | `scout-stack.yml` (CloudFormation/stack def) |
| `scripts/` | 2 shell scripts | deploy env + DATABASE_URL resolution |

### `apps/workspaces` — 4,300 LOC. Workspaces, tenancy, schema lifecycle, background tasks

| File | LOC | Responsibility |
|---|---|---|
| `tasks.py` | 1,289 | ALL procrastinate tasks: `materialize_workspace`, `refresh_tenant_schema`, `resume_thread_after_materialization`, `expire_inactive_schemas` (cron */30), `expire_stale_thread_jobs` (janitor, cron */15), `teardown_schema`, `teardown_view_schema_task`, `rebuild_workspace_view_schema`, sibling-view-schema rebuild helpers, ThreadJob reconciliation against the procrastinate job table |
| `services/schema_manager.py` | 631 | Physical schema create/teardown, readonly roles, view-schema builds (18 functions) |
| `api/workspace_views.py` | 617 | Workspace CRUD, members, tenants, list/detail |
| `api/views.py` | 540 | Data dictionary, table detail, refresh schema + refresh status |
| `models.py` | 292 | `TenantSchema` (state machine), `MaterializationRun` (state machine), `Workspace`, `WorkspaceTenant`, `WorkspaceMembership` (roles), `WorkspaceViewSchema` (state machine), `TenantMetadata` |
| `api/materialization_views.py` | 212 | cancel / retry endpoints |
| `api/jobs_views.py` + `jobs_cancel.py` | 243 | ThreadJob status + cancel endpoints |
| `services/workspace_service.py` | 110 | auto-create workspace logic |
| `workspace_resolver.py` | 60 | request → workspace resolution |
| `permissions.py` | 41 | role permission classes (v1 reviews flagged as largely unenforced) |
| `management/commands/` | 158 | `purge_synced_data`, `backfill_readonly_roles` |

### `apps/users` — 3,109 LOC. Auth, OAuth providers, tenant resolution, merge

| File | LOC | Responsibility |
|---|---|---|
| `views.py` | 419 | tenant list/select/ensure, connections CRUD, api-key providers |
| `services/merge.py` | 355 | duplicate-user merge (email-verification-gated) |
| `auth_views.py` | 274 | csrf/login/logout/me/signup/providers/disconnect |
| `models.py` | 251 | `User`, `TenantMembership` (+per-chatbot team fields), `TenantConnection` (replaced `TenantCredential` 2026-06-05) |
| `services/tenant_resolution.py` | 204 | provider payload → tenant memberships |
| `signals.py` | 135 | allauth signal handlers (login-time reconciliation) |
| `adapters.py` | 122 | allauth adapters |
| `services/credential_resolver.py` | 112 | connection-based credential resolution, fail-closed OAuth team guard; async variant `aresolve_credential` |
| `services/token_refresh.py` | 93 | OAuth token refresh (sync, wrapped) |
| `services/api_key_providers/` | ~180 | OCS + CommCare API-key team detection (`ocs_team.py` carries TODO(OCS #3586) stopgap) |
| `providers/commcare/` | ~90 | custom allauth provider |
| `management/commands/` | ~150 | `setup_oauth_apps`, `merge_duplicate_users` |

### `apps/agents` — 2,626 LOC. LangGraph agent

| File | LOC | Responsibility |
|---|---|---|
| `graph/base.py` | 793 | agent graph build, schema-context assembly into system prompt, panic-loop circuit breaker, MCP tool schema rewriting (hides `tenant_id` etc. from LLM), tool wiring |
| `tools/artifact_tool.py` | 358 | create/update artifact tools (LLM-facing) |
| `tools/learning_tool.py` | 258 | save_learning tool |
| `tools/recipe_tool.py` | 255 | save_recipe tool |
| `prompts/` | ~390 | `base_system.py`, `artifact_prompt.py` |
| `memory/checkpointer.py` | 166 | PostgreSQL LangGraph checkpointer via AsyncConnectionPool |
| `graph/state.py` | 148 | agent state dataclasses, ThreadJob references |
| `tracing.py` | 98 | Langfuse instrumentation |
| `mcp_client.py` | 90 | MCP client to `MCP_SERVER_URL` |

### `apps/artifacts` — 2,157 LOC

| File | LOC | Responsibility |
|---|---|---|
| `views.py` | 988 | list/detail/undelete/sandbox/data/query-data/export; sandboxed React rendering; live query execution against tenant schemas |
| `services/export.py` | 472 | export formats (incl. browser print-to-PDF path) |
| `models.py` | 222 | `Artifact`: `code` (TextField of React source), `source_queries` (JSONField of SQL), versioning, soft delete |

### `apps/chat` — 1,230 LOC

| File | LOC | Responsibility |
|---|---|---|
| `thread_views.py` | 257 | thread list/messages/share/viewed + public shared-thread view |
| `views.py` | 249 | `/api/chat/` streaming endpoint (workspace_id in body) |
| `stream.py` | 249 | SSE stream assembly from LangGraph events |
| `helpers.py`, `message_converter.py`, `checkpointer.py`, `rate_limiting.py` | ~340 | supporting plumbing |
| `models.py` | 94 | `Thread` (share_token), `ThreadJob` (state machine; unique `procrastinate_job_id`, `tool_call_id`) |

### `apps/recipes` — 1,324 LOC
`models.py` 438 (Recipe with `prompt_template` TextField, RecipeRun, share tokens); `services/runner.py` 343 (re-invokes agent graph with templated prompt); `api/views.py` 185.

### `apps/knowledge` — 980 LOC
`api/views.py` 318 (CRUD + import/export); `models.py` 202 (`TableMetadata` keyed by free-text `table_name`, `KnowledgeEntry`, `Learning` with `original_sql`/`corrected_sql`/`applies_to_tables`); `services/retriever.py` 140.

### `apps/transformations` — 1,236 LOC
`services/commcare_staging.py` 349 (generates dbt staging SQL from CommCare metadata); `models.py` 193 (`TransformationAsset`, `TransformationRun`); `services/executor.py` 185 (runs dbt); `services/lineage.py` 136; `services/dbt_project.py` 65; `views.py` 166 (DRF ViewSets — the only DRF-router surface in the codebase).

### `apps/common` — 8 LOC. Effectively empty (`utils.py`).

### `mcp_server/` — 5,853 LOC. Standalone FastMCP process (port 8100)

| File | LOC | Responsibility |
|---|---|---|
| `services/materializer.py` | 1,972 | **largest file in the repo**; 33 functions; per-table writer functions for all three providers; three-phase discover/load/transform orchestration; cursor watermarks for resume; catalog reconciliation |
| `server.py` | 982 | 11 MCP tool definitions (see §3) |
| `services/sql_validator.py` | 401 | SQL allow-list validation for `query` tool |
| `services/metadata.py` | 359 | pipeline-driven list_tables/describe_table/get_metadata |
| `loaders/` (19 files) | ~1,400 | provider API loaders: CommCare (forms, cases, metadata), Connect (visits, users, payments, invoices, assessments, completed_works, completed_modules, metadata), OCS (sessions, messages, participants, experiments, metadata) + 3 base classes |
| `services/query.py` | 172 | read-only query execution: `SET ROLE` readonly role + `SET search_path` to tenant schema |
| `services/dbt_runner.py` | 175 | programmatic dbtRunner, threading.Lock |
| `pipeline_registry.py` | 172 | YAML pipeline registry (`pipelines/*.yml`) |
| `context.py` | 163 | per-call tenant context resolution (high churn: 19 touches) |
| `envelope.py` | 116 | uniform tool response envelope |
| `auth.py` | small | MCP authz (v1 reviews: "trust the caller" model) |

### `frontend/src/` — 13,085 LOC, 117 files

| File | LOC | Responsibility |
|---|---|---|
| `pages/WorkspaceDetailPage/` | 1,045 | **largest frontend file**; workspace settings, data-sources tab, members, materialization controls |
| `components/WorkspaceSwitcher/` | 555 | switcher redesigned for "hundreds of workspaces" |
| `components/ChatMessage/` + `ToolOutput` | 765 | message rendering incl. tool cards |
| `pages/ConnectionsPage/` | 409 | TenantConnection management |
| `components/ChatPanel/` | 332 | chat input/stream handling |
| `components/ArtifactPanel/` | 329 | artifact rendering host |
| `pages/PublicThreadPage`, `PublicRecipeRunPage`, `PublicRecipePage` | 639 | unauthenticated share-token pages |
| `store/` (slices) | ~700 | hand-rolled state slices: dictionary, knowledge, recipe (+ workspace/chat) |
| `api/workspaces.ts` etc. | ~400 | hand-written TS types mirroring API shapes (no codegen — seam §4) |
| `hooks/useWorkspaceThreadSync.ts` | 124 | workspace↔thread URL sync (site of the cross-workspace threadId bug, fixed 00c423d) |

### `config/`
`settings/{base,development,production,test}.py` (base 351 LOC, 39 touches); `urls.py` (25 touches); `deploy.yml` + `deploy-worker.yml` — **two separate Kamal deploy configs** (api vs worker; 19 + 14 touches); `views.py` (`widget_js_view` — embed widget SDK served at `/widget.js`).

---

## 2. Git churn analysis (full history)

### Headline numbers
- 680 non-merge commits over ~4 months. Monthly: Feb 244, Mar 75, Apr 130, May 185, Jun 46 (partial).
- **Fix-prefixed commits: 179/680 ≈ 26%.** Subject-line grep for "fix" anywhere: 172–230 depending on pattern — roughly **1 in 4 commits is a fix**.
- Fix-dense days: 2026-02-19 (18 fixes), 2026-03-26 (17), 2026-05-21 (14), 2026-02-12 (11), 2026-05-20 (9), 2026-06-10 (8).

### Per-file touch counts (top, full history)

| Touches | File |
|---|---|
| 59 (36 in fix commits) | `mcp_server/server.py` |
| 47 (27) | `apps/agents/graph/base.py` |
| 45 (27) | `apps/chat/views.py` |
| 39 (13) | `config/settings/base.py` |
| 37 (24) | `apps/workspaces/tasks.py` |
| 30 (17) | `mcp_server/services/materializer.py` |
| 30 (14) | `apps/artifacts/views.py` |
| 27 (12) | `apps/users/views.py` |
| 26 | `frontend/src/components/Sidebar/Sidebar.tsx` |
| 22 | `frontend/src/components/ChatPanel/ChatPanel.tsx` |
| 19 (11) | `mcp_server/context.py` |
| 19 | `config/deploy.yml`; 18 `.kamal/secrets`; 14 `config/deploy-worker.yml` |

Interpretation for reviewers: churn concentrates on the **chat↔MCP↔worker spine**
(`server.py`, `graph/base.py`, `chat/views.py`, `tasks.py`, `materializer.py`) — the
same files that dominate fix commits. `mcp_server/context.py` at 11 fix-touches in 163
LOC is the highest fix density per line in the repo.

### Fix-chain clusters (commits orbiting one mechanism)

1. **ThreadJob / resume-after-materialization** — 19 commits, 2026-05-20 → 05-28:
   model added, fire-and-ack, chained resume task, then a chain of tightenings:
   idempotency, CAS to prevent duplicate `ainvoke`, restore CANCELLED path, janitor
   reorder, dedupe + robustness, stale resume cursor after intervening COMPLETED run,
   oauth_tokens through resume config, bounded `ainvoke` with timeout + synthetic
   failure message. The 1,016-line `tests/test_resume_thread_task.py` is the residue.
2. **Materialization atomicity / catalog truth** — 2026-05-27 → 05-28: atomicity +
   catalog reconciliation (#185), pre-loop run failure handling, STALE flip deferred to
   teardown, `row_count` → `materialized_row_count` + `row_count_verified`, resumable
   materialization via per-page cursor watermark (#187), silent row duplication on
   page-replay for 5 Connect tables.
3. **View-schema lifecycle (multi-tenant workspaces)** — 2026-05-27 + 2026-06-10:
   teardown when workspace drops to single-tenant; truncation-safe view names +
   idempotent rebuild; surface build failures to agent/MCP/status API; sibling-schema
   consistency across rematerialization/teardown (PRs #227–#230, all post-incident).
4. **Worker connection hygiene** — 2026-06-10: survive dead worker DB connections,
   custom task decorator explicitly marked **temporary pending upstream procrastinate
   fix** (`ab4b426`), thread_sensitive + after-task reset mirroring upstream.
5. **OCS connections rebuild** — 2026-06-05: 10-commit feature chain replacing
   `TenantCredential` with `TenantConnection` (PR #220; an earlier attempt #214 was
   rejected as hallucinated per project memory).
6. **Agent panic loops** — 2026-05-27: circuit breaker for contradictory schema
   responses (#190), system prompt aligned with actual SQL allow-list.
7. **Early high-fix bursts** — 2026-02-12/19 and 2026-03-26 predate most current
   structure; useful to the git historian for "fixed-where-it-bit" sweeps.

### Rename / migration events (and what they can leave behind)

| When | Event | Residue risk |
|---|---|---|
| 2026-02-16 | `datasources` app removed, DB connections consolidated into `projects` | stale references in plans/docs |
| 2026-02-17 | SQLValidator moved into MCP server; dead local data-access tools removed | split-brain validation assumptions |
| 2026-03-12 | **migrations reset** (#85) | history before this is invisible to `makemigrations` archaeology |
| 2026-03-16 | "god module split, workspace resolver, caching" hardening (#87) | callers of pre-split paths |
| 2026-03-17 | **`projects` app renamed to `workspaces`** (#89) | v1 reviews still found "projects" residue; `apps/projects/*` shows in churn stats |
| 2026-05-01 | **Celery → Procrastinate migration** | docs/plans reference celery; task semantics changed |
| 2026-05-27 | `row_count` → `materialized_row_count` | consumers of the old key |
| 2026-05-29 | UI rename "tenants" → "data sources" | UI/API vocabulary split (API still says tenants) |
| 2026-06-04 | public/share-creation **UI removed** (chat share menu + recipe public link) | backend share endpoints + public pages still live (see §3, §4) |
| 2026-06-05 | `TenantCredential` removed, superseded by `TenantConnection` | migration mapped credentials onto connections; mock/test residue |

---

## 3. Feature inventory (every user-facing surface + entry point)

### HTTP routes (Django, `config/urls.py` + per-app urls)

**Top-level**
- `GET /` — api_root HTML splash (`config/urls.py`)
- `GET /widget.js` — embed widget SDK (`config/views.py:widget_js_view`)
- `GET /health/` — `apps.workspaces.views.health_check`
- `/admin/` — Django admin (admin.py registered in workspaces, artifacts, recipes, knowledge, transformations, users)
- `/accounts/...` — allauth (Google/GitHub/CommCare OAuth flows)

**Auth (`/api/auth/`, `apps/users/auth_urls.py`)**
`csrf/`, `me/`, `login/`, `logout/`, `providers/`, `providers/<id>/disconnect/`,
`signup/`, `tenants/`, `tenants/select/`, `tenants/ensure/`, `connections/`,
`connections/<id>/`, `api-key-providers/`

**Chat**
- `POST /api/chat/` — streaming SSE (workspace_id in body) — `apps/chat/views.py:chat_view`
- `GET /api/chat/threads/shared/<share_token>/` — **public, no auth** — `thread_views.public_thread_view`

**Workspaces (`/api/workspaces/`)**
- list, `<uuid>/` detail; `<uuid>/members/` (+`<id>/`), `<uuid>/tenants/` (+`<uuid>/`)
- `<uuid>/data-dictionary/` (+`tables/<qualified_name>/`)
- `<uuid>/refresh/` + `refresh/status/` — the legacy refresh path flagged S1 by v1 run A
- `<uuid>/materialization/cancel/`, `<uuid>/materialize/retry/`
- `<uuid>/jobs/active/`, `<uuid>/jobs/<uuid>/cancel/`
- `<uuid>/threads/` (+`<uuid>/messages|share|viewed/`)
- `<uuid>/artifacts/` — list, detail, undelete, sandbox, data, query-data, export/<format>
- `<uuid>/recipes/` — list, detail, run, runs, run detail
- `<uuid>/knowledge/` — list/create, export, import, detail

**Other**
- `/api/transformations/assets/`, `/api/transformations/runs/` (DRF router — only ViewSet surface)
- `GET /api/recipes/runs/shared/<share_token>/` — **public, no auth**

### Frontend routes (`frontend/src/router.tsx`)
`/` (home), `/chat` (redirect), `/workspaces` (list), `/workspaces/:workspaceId`
(detail; also pretty `/:slug/:workspaceId` variants), `/workspaces/:workspaceId/chat[/:threadId]`
(also slug variants), `/artifacts`, `/knowledge[/new|/:id]`, `/recipes[/:id[/runs/:runId]]`,
`/data-dictionary`, `/settings/connections`, `*` → `/`.
Public pages exist as components (`PublicThreadPage`, `PublicRecipePage`,
`PublicRecipeRunPage`) — reviewers should check how they are routed/served since
share-creation UI was removed 2026-06-04.

### MCP tools (`mcp_server/server.py`, 11 tools)
`list_tables`, `describe_table`, `get_metadata`, `get_lineage`, `query` (SQL,
allow-list validated), `list_pipelines`, `get_materialization_status`,
`cancel_materialization`, `run_materialization` (fire-and-ack via ThreadJob),
`get_schema_status`, `teardown_schema` (confirm flag).
All tenant-scoped tools take `workspace_id: str = ""` — context injection happens in
`apps/agents/graph/base.py` (`MCP_TOOL_NAMES`, `_llm_tool_schemas` hides params from LLM).

### Agent-native tools (`apps/agents/tools/`)
`create_artifact_tools` (create/update artifact), `create_save_learning_tool`,
`create_recipe_tool` — these write platform-DB rows directly (no MCP hop).

### Management commands
`setup_oauth_apps`, `merge_duplicate_users` (users);
`purge_synced_data`, `backfill_readonly_roles` (workspaces).

### Periodic tasks (procrastinate cron, `apps/workspaces/tasks.py`)
- `expire_inactive_schemas` — `*/30 * * * *` (TTL janitor; implicated in 2026-06-10 incident)
- `expire_stale_thread_jobs` — `*/15 * * * *` (ThreadJob janitor; reconciles against procrastinate's own job table via `_procrastinate_job_status`)

### Pipelines (`pipelines/*.yml` → `mcp_server/pipeline_registry.py`)
`commcare_sync.yml`, `connect_sync.yml`, `ocs_sync.yml` — declare sources, loader
references, dbt model lists. Loader implementations in `mcp_server/loaders/`.

---

## 4. Seam inventory

### Process boundaries (5 processes + 2 DB planes + 3 external providers)

```
React SPA (5173/3000)
   │ JSON + SSE; hand-written TS types (no codegen)
Django API (8000, ASGI/uvicorn)
   │ MCP protocol (streamable HTTP, MCP_SERVER_URL)        │ procrastinate defer
MCP server (8100, FastMCP) ──────────────────────────── Worker (manage.py procrastinate)
   │ psycopg direct                                        │ Django ORM + direct DDL
Managed data DB (tenant schemas, view schemas) ──── Platform DB (Django models,
   │                                                LangGraph checkpoints, procrastinate queue)
CommCare API · Connect API · OCS API  (OAuth tokens / API keys via TenantConnection)
```

Boundary notes for seam reviewers:
- **API ↔ MCP**: agent calls MCP tools; MCP server *also* writes Django-modeled rows
  (`MaterializationRun`, `ThreadJob`) via its own DB access — two codebases share one
  schema without sharing the ORM layer (`mcp_server/server.py`, `services/materializer.py`).
- **MCP ↔ worker**: `run_materialization` fires-and-acks; worker chains
  `resume_thread_after_materialization`; resume protocol carries oauth_tokens through
  task config. `tasks.py:373` TODO: "cleaner fix is to let MCP write a placeholder
  ThreadJob *before*..." — an acknowledged race at this seam.
- **Worker ↔ procrastinate internals**: janitor reads procrastinate's job table by
  `procrastinate_job_id` to reconcile ThreadJob/MaterializationRun state; custom task
  decorator (connection hygiene) is marked temporary pending upstream fix.
- **Platform DB ↔ managed DB**: Django state rows (`TenantSchema`, `WorkspaceViewSchema`)
  vs. physical schemas created/dropped by `schema_manager.py` and the materializer;
  reconciliation is manual (catalog reconciliation added #185 after drift bit).
- **Deploy plane**: two Kamal configs (`config/deploy.yml` api, `deploy-worker.yml`
  worker) — same image, divergent env wiring; `.kamal/secrets` churned 18×.

### Stored free-text references to schema objects (break silently on rename/teardown)

| Store | Field(s) | Written by | Resolved against |
|---|---|---|---|
| `Artifact` | `source_queries` (JSON of SQL), `code` (React source that fetches via query-data) | agent artifact tool | tenant/view schemas at view time (`artifacts/views.py` data/query-data) |
| `Recipe` | `prompt_template` (may name tables) | agent recipe tool / API | agent re-run at recipe run time |
| `TableMetadata` | `table_name` (free-text key), `related_tables` JSON, `column_notes` JSON | knowledge API / agent | catalog by string match |
| `Learning` | `original_sql`, `corrected_sql`, `applies_to_tables` | save_learning tool | retriever string match |
| `Workspace` | `system_prompt`, `data_dictionary` JSON (+`data_dictionary_generated_at`) | refresh path / users | prompt assembly in `graph/base.py` |
| LangGraph checkpoints | serialized messages containing SQL + table names | checkpointer | replayed on thread resume |

### Multi-writer state columns (shared-row contention map)

| State column | Writer modules (non-test) |
|---|---|
| `MaterializationRun.state` (STARTED/…/STALE) | `mcp_server/services/materializer.py`, `mcp_server/server.py` (cancel), `apps/workspaces/tasks.py` (incl. janitors), `api/materialization_views.py` (cancel/retry), `api/views.py` (refresh), `management/commands/purge_synced_data.py` |
| `TenantSchema.state` | `services/schema_manager.py`, `tasks.py` (TTL janitor, teardown, provision-resurrect), `api/views.py`, `workspace_service.py`, `mcp_server/context.py` (touch on access), management commands |
| `ThreadJob.state` (PENDING/…) | `mcp_server/server.py` (create on fire-and-ack), `tasks.py` (resume task, janitor), `api/jobs_cancel.py`, `api/materialization_views.py`, `agents/graph/state.py` |
| `WorkspaceViewSchema.state` + `last_error` | `schema_manager.py`, `tasks.py` (rebuild/teardown/sibling rebuild, `_fail_dependent_view_schemas`) |
| `TenantSchema.last_accessed_at` / `WorkspaceViewSchema.last_accessed_at` | MCP context touch, provision path, TTL janitor reads — the exact triangle of the 2026-06-10 TTL incident |

Readers that derive user-facing status from combinations of the above:
`api/jobs_views.py`, `api/views.py` (refresh status), `_aggregate_materialization_state`
in `tasks.py`, frontend `MaterializationProgressBanner`, and the agent prompt assembly.

---

## 5. Symptom seeds (known incidents, fixes, and acknowledged debt)

Harvested from git subjects, PR history, the two v1 review reports, project memory
notes, and in-code TODOs. Each seed is a *steering hint*: either the area is now fine
(verify the fix) or siblings of the bug survive.

1. **2026-06-10 prod incident cluster** (PRs #227–#232, all merged 2026-06-11):
   (a) 63-byte view-name truncation collisions broke multi-tenant view-schema builds;
   (b) TTL janitor dropped freshly-materialized schemas — provision resurrected EXPIRED
   `TenantSchema` rows without touching `last_accessed_at`;
   (c) frontend carried `threadId` across workspace switches;
   (d) view-schema build failures were swallowed — agent was told "completed".
2. **2026-06-09 prod**: worker's Django DB connection died after RDS backup window and
   never reconnected; all tasks + both janitors dead ~22h; UI stuck at "Preparing…".
   Fix: custom task decorator (connection hygiene) explicitly **temporary pending
   upstream procrastinate fix** (`ab4b426`) + API-side staleness backstop.
3. **2026-05-30**: materialization leaked zombie procrastinate `doing` jobs when the
   worker died; janitor could not rescue them.
4. **Resume/ThreadJob fix chain** (19 commits, 2026-05-20→28): duplicate `ainvoke`
   races (CAS added), stale resume cursor after an intervening COMPLETED run, CANCELLED
   path regression, janitor ordering, oauth_tokens threading. `tasks.py:373` TODO
   admits a remaining create-order race (MCP should pre-create the ThreadJob).
5. **Connect loader data integrity**: silent row duplication on page-replay for 5
   Connect tables (`f26c1a0`); Connect writers missing-id crash (`2587158`); bounded
   retry for Connect 5xx added 2026-05-27. Sibling loaders (OCS, CommCare) deserve the
   same questions.
6. **Catalog truth**: `row_count` renamed `materialized_row_count` + `row_count_verified`
   flag; catalog reconciliation added after drift (#185); v1 reviews counted **five
   table-catalog implementations that can disagree**.
7. **Legacy `/refresh/` path** — v1 run A's S1: refresh loads data into the old schema
   then destroys it (`apps/workspaces/tasks.py:refresh_tenant_schema` + `/refresh/`
   route still wired). Also the site of a SynchronousOnlyOperation fix (`8104ce1`);
   project memory: mocking `aresolve_credential` hid that bug in tests.
8. **Roles unenforced** — v1 both runs: `workspaces/permissions.py` classes ~dead;
   `backfill_readonly_roles` exists but route-level enforcement is partial.
9. **Merge/verification** — safe user-merge is gated on email verification that never
   happens for some providers; resolved operationally 2026-06 (refusal is
   secure-by-design), but the merge path remains subtle (`users/services/merge.py`).
10. **Share surface drift**: share-creation UI removed 2026-06-04, but public
    endpoints (`/api/chat/threads/shared/`, `/api/recipes/runs/shared/`), share
    fields, public pages, and `/widget.js` remain live.
11. **Agent panic loops** on contradictory schema responses (#190) — circuit breaker
    added in `graph/base.py`; prompt was realigned with the actual SQL allow-list
    (`93504d5`) — prompt↔validator drift is a recurring class.
12. **Input-validation family**: connect-name truncation (v1), 63-byte identifier
    truncation (incident 1a) — identifier length/shape bugs recur across providers.
13. **OCS team detection stopgap**: `users/services/ocs_team.py` TODO(OCS #3586) —
    sessions-based detection awaiting upstream endpoint; fail-closed guard added.
14. **TODO.md security section unchecked**: per-tenant PostgreSQL role isolation
    (partially done via SET ROLE?), append-only MCP audit table, loader network
    egress restriction — all still `[ ]`.
15. **Model/config drift**: temperature param removed as unsupported on Opus 4.7+
    (`378121e`); agent model made configurable defaulting to claude-opus-4-8 — config
    knobs vs. deployed reality worth a check.
16. **Faked progress UI** (project norm): progress bars must show real denominators
    (`87df4ee`) — any remaining indeterminate-but-fake indicators are regressions.

---

## 6. Proposed roster additions

The standing roster (10 verticals, 10 lenses, 5 seams, 3 journey tracers, 3 generalists,
1 git historian) covers most of what the map shows. Three gaps:

1. **Vertical: provider data loaders & materializer writers** (`kind: vertical`).
   The mcp-server vertical owns tools/envelope/authz; nobody owns the **19 loader
   files + the 1,972-line materializer with its 33 functions and per-table hand-cloned
   writers** — the single largest file in the repo, three external API contracts
   (CommCare/Connect/OCS pagination, id semantics, error shapes), and the site of two
   confirmed data-integrity bugs (row duplication, missing-id crash) whose siblings in
   the other ~14 loaders were never audited. This is both "subsystem bigger than
   expected" and "third-party integration nobody owns".

2. **Seam: Django job-state rows ↔ procrastinate internals** (`kind: seam`).
   Distinct from the chat↔MCP↔worker seam: `ThreadJob`/`MaterializationRun` carry
   `procrastinate_job_id` foreign references into procrastinate's own queue table; the
   janitor's correctness depends on procrastinate job-status semantics
   (`_procrastinate_job_status`, `tasks.py:693`); the connection-hygiene decorator is
   explicitly temporary pending an upstream fix; two incidents (zombie `doing` jobs,
   dead-connection 22h outage) lived exactly here. The standing seams treat the worker
   as a black box; this reviewer owns the contract with the queue library itself.

3. **Lens: async/sync boundary & DB-connection lifecycle** (`kind: lens`).
   A recurring, codebase-specific failure class not named in the ten standing lenses:
   SynchronousOnlyOperation bugs (8104ce1; one was masked by test mocking),
   `sync_to_async` policy exceptions, `thread_sensitive` handling, AsyncConnectionPool
   (checkpointer) vs. Django ORM connections vs. psycopg-direct (MCP) — three
   connection regimes in one process family, with a 22-hour outage already attributable
   to connection-lifecycle mishandling. Hunt every async/sync crossing and every
   connection acquire/release path everywhere.

Not proposed (considered, already covered): embed widget + public share surface
(tenancy/sharing vertical + authz lens + journey tracers); Kamal/deploy drift (ops
lens); Langfuse/Sentry/TaskBadger (observability lens); dbt (transformations vertical).
