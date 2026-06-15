# Scout Architecture Review — Generalist 3

*Reviewer: generalist-3 (independent full-codebase pass, no cartography input).*
*Evidence standards: per docs/arch-review-methodology.md §Shared evidence standards. Every finding carries status, impact, complexity (essential vs accidental), and confidence. BROKEN-NOW claims include the full entry-point → consequence chain.*

---

## 1. Executive summary

Scout's core chat → MCP → materialization loop is in much better shape than the rest of the system. The June 2026 incident response visibly hardened the materialization state machine, the ThreadJob lifecycle, and the janitor/reconcile layering — that code is now careful, CAS-guarded, and well-commented, and the comments match the code. The problem is everything *around* that hot path: features that were built to demo quality and then abandoned have silently broken as the core evolved underneath them, and nothing detected it because tests mock exactly the seams that drifted.

Three findings are flat-out broken today on user-reachable paths:

1. **The Data Dictionary "Refresh" button destroys the workspace's data.** The refresh task creates a new `_r<hex>` schema, but the pipeline it invokes provisions and loads into the *base* schema; the task then marks the empty `_r` schema ACTIVE and schedules teardown of the base schema — the one that just received the fresh data. Thirty minutes after a refresh, the data is dropped and the catalog points at an empty schema.
2. **Recipe runs cannot work at all.** `RecipeRunner` calls `build_agent_graph(tenant_membership=…)` — a parameter that was removed when the graph moved to workspace scoping (~PR #79/#89 era). Every run raises `TypeError`, the API returns 500, and the `RecipeRun` row is stranded in RUNNING forever. The test suite mocks `build_agent_graph` with a bare `Mock()`, so the signature mismatch is invisible.
3. **Shared/public threads never show their artifacts.** Artifacts are looked up by `conversation_id`, but the tool factory is called without one, so every artifact is created with `conversation_id=""`.

Beyond these, the **artifact "sandbox" is not a sandbox** (`allow-scripts allow-same-origin` on a same-origin iframe, with CSP `connect-src 'self'` and a JS-readable CSRF cookie — LLM-generated code runs with the user's full API authority), **workspace roles are unenforced for most content types** (a `read` member can delete artifacts, knowledge, and recipes), and the **agent-facing teardown tool implements different semantics than the worker-task teardown of the same name**, leaving Django state rows claiming schemas exist after it drops them — including tenant schemas shared with *other* workspaces.

The deep cause is consistent: the system went through three scoping generations (project → tenant/domain → workspace) and a materialization redesign, and each migration moved the hot path while leaving satellites (recipes, refresh, artifacts-by-thread, oauth-token plumbing, frontend "domains") wired to the previous generation. There is no compile-time or test-time enforcement of the cross-module contracts, so drift is only discovered when a user presses the button.

The genuinely complicated requirements (multi-tenant workspaces sharing physical tenant schemas, TTL'd schema lifecycle, background resume into LangGraph threads) are handled with mostly *essential* complexity in `tasks.py`/`materializer.py`/`schema_manager.py`. The accidental complexity lives in the duplication: four different "which schema does this workspace query" resolvers, two teardown implementations, two artifact data mechanisms, three naming strata.

## 2. As-built architecture map

**Processes** (Kamal deploys, separate containers): Django ASGI API (`config/`, `apps/`), standalone FastMCP server (`mcp_server/`, streamable-http on :8100, internal network only), Procrastinate worker (same codebase, `manage.py procrastinate worker`), Vite/React SPA (`frontend/`), platform Postgres (Django state + procrastinate + LangGraph checkpoints) and a "managed" Postgres (materialized tenant data; same DB in dev).

**Identity & tenancy** (`apps/users`, `apps/workspaces`): `User` (email-keyed, allauth OAuth: commcare, commcare_connect, ocs) → `TenantConnection` (one credential) → `TenantMembership` (user↔`Tenant`, provider metadata e.g. OCS team) — created by post-login resolution calls to provider APIs (`tenant_resolution.py`). A `post_save` signal on TenantMembership auto-creates a single-tenant `Workspace` + MANAGE membership. Workspaces are M2M to tenants via `WorkspaceTenant`; `WorkspaceMembership` carries a role (`read`/`read_write`/`manage`).

**Physical data layer**: each tenant gets one shared Postgres schema (`_sanitize_schema_name(external_id)`, tracked by `TenantSchema` rows with a PROVISIONING/ACTIVE/MATERIALIZING/EXPIRED/TEARDOWN/FAILED state machine). Multi-tenant workspaces additionally get a `ws_<hash16>` schema of `{prefix}__{table}` views (`WorkspaceViewSchema`, built by `SchemaManager.build_view_schema`). Per-schema `*_ro` Postgres roles enforce read-only query access. TTL janitor (`expire_inactive_schemas`, 24h) tears down idle schemas; `touch_workspace_schemas` resets TTLs on chat activity.

**Materialization** (`mcp_server/services/materializer.py` + `apps/workspaces/tasks.py`): three-phase Discover→Load→Transform, driven per-tenant from YAML pipeline configs (`pipelines/*.yml`, registry in `mcp_server/pipeline_registry.py`), loaders per provider (`mcp_server/loaders/`). Progress + cancellation flow through `MaterializationRun.progress/state` with CAS updates. Chat-initiated runs go MCP tool `run_materialization` → `materialize_workspace` task → `resume_thread_after_materialization` task, which re-invokes the agent with a system-framed message. `ThreadJob` rows tie procrastinate jobs to chat threads; a worker-side janitor plus an API-side poll backstop reconcile stuck jobs.

**Agent** (`apps/agents`, `apps/chat`): `chat_view` (raw async Django view, SSE) builds a LangGraph graph per request (`build_agent_graph(workspace, user, …)`): agent ↔ ToolNode loop with an escalation circuit-breaker; MCP tools loaded fresh per request from the MCP server; workspace_id/user_id/thread_id injected server-side into MCP tool calls (hidden from the LLM schema); AsyncPostgresSaver checkpointer is the source of truth for conversation history (Thread rows are just an index). System prompt = base + artifact instructions + workspace prompt + full knowledge dump + pre-fetched schema context (60s TTL cache).

**Content satellites**: artifacts (sandbox iframe rendering, static `data` vs live `source_queries`), recipes (prompt templates + runner), knowledge (TableKnowledge/KnowledgeEntry/AgentLearning → prompt), transformations (TransformationAsset staging-pipeline, lineage), data-dictionary API (a parallel, DRF-sync implementation of the same catalog the MCP server exposes).

**Frontend**: Zustand store slices; workspace selection is a global `activeDomainId` ("domain" = workspace, a leftover naming stratum); content pages (`/artifacts`, `/recipes`, `/knowledge`, `/data-dictionary`) are not workspace-scoped in the URL and key off the global selection.

## 3. Findings

### F1. Data Dictionary "Refresh" loads data into the old schema, then destroys it — `BROKEN-NOW / data-loss / verified-by-trace / accidental`

The refresh path predates `run_pipeline`'s internal `provision()` and was never migrated.

Chain (every hop read in current code):

1. UI: `frontend/src/store/dictionarySlice.ts:197` — `api.post('/api/workspaces/${activeDomainId}/refresh/')` (Data Dictionary page, `refresh-schema-btn`, `DataDictionaryPage.tsx:36/101`).
2. `apps/workspaces/api/views.py:352-365` `RefreshSchemaView.post` → `SchemaManager().create_refresh_schema(tenant)` → `apps/workspaces/services/schema_manager.py:169-181` creates TenantSchema `"{base}_r{uuid8}"` in PROVISIONING, then defers `refresh_tenant_schema`.
3. `apps/workspaces/tasks.py:126-155` task creates the *physical* `_r` schema (`create_physical_schema`).
4. `apps/workspaces/tasks.py:173` → `run_pipeline(membership, credential, pipeline_config)` — **no target schema is passed**. `mcp_server/services/materializer.py:183` `run_pipeline` calls `SchemaManager().provision(tenant_membership.tenant)`, which resolves `schema_name = _sanitize_schema_name(tenant.external_id)` — the **base** schema (`schema_manager.py:57-129`) — and loads all data there. The `MaterializationRun` is attached to the base `TenantSchema` row.
5. `apps/workspaces/tasks.py:182-184` marks the **empty** `_r` schema ACTIVE.
6. `apps/workspaces/tasks.py:188-197` flips every *other* ACTIVE schema for the tenant — i.e. the base schema that just received the fresh data — to TEARDOWN and schedules `teardown_schema` in 30 minutes.
7. `apps/workspaces/tasks.py:609-663` `teardown_schema` drops the base schema (`DROP SCHEMA … CASCADE`), flips its COMPLETED runs to STALE, and fails dependent sibling view schemas.

Consequence: ~30 minutes after any refresh, the tenant's materialized data is gone for **every workspace sharing that tenant**; the catalog (`_resolve_tenant_schema` → the ACTIVE `_r` row, which has no runs and no tables) shows nothing; `RefreshStatusView` reports the `_r` row as `active` (i.e. "success"). The empty `_r` schema also accumulates: each refresh leaves another ACTIVE orphan row until the TTL janitor collects it.

Note: the agent/chat materialization path (`materialize_workspace`) does *not* use this indirection and is correct — only the REST refresh path is broken. `schema_manager.py:174` still says the dispatched task is "the Celery task", a fossil that dates the path.

### F2. Recipe execution is impossible — signature drift to a removed parameter — `BROKEN-NOW / correctness / verified-by-trace / accidental`

Chain:

1. UI: `frontend/src/store/recipeSlice.ts:135` → `POST /api/workspaces/<ws>/recipes/<id>/run/`.
2. `apps/recipes/api/views.py:99-110` `RecipeRunView.post` → `RecipeRunner(...).execute()`.
3. `apps/recipes/services/runner.py:188` `execute()` → `async_to_sync(self._build_graph)()` — **outside** the try/except.
4. `apps/recipes/services/runner.py:115-119` `_build_graph` calls `build_agent_graph(tenant_membership=self._tenant_membership, user=…, checkpointer=None)`.
5. `apps/agents/graph/base.py:480-486` — `build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)`. There is no `tenant_membership` parameter and `workspace` is required → `TypeError` on every call.
6. The view's catch (`api/views.py:109-111`) returns the raw TypeError string as a 500. The `RecipeRun` row, created earlier at `runner.py:189/_create_run_record`, is left in `RUNNING` forever (no janitor exists for RecipeRun).

Even if the signature matched, the runner is two more generations behind: its `initial_state` uses `tenant_id` / `tenant_name` / `tenant_membership_id` (`runner.py:198-208`, `285-295`) while `AgentState`/the injection node use `workspace_id`/`thread_id`; and it passes no `mcp_tools`, so the agent would have no data tools. The `save_as_recipe` agent tool and recipe CRUD work — users can create recipes they can never run. Git context: the graph moved to workspace scoping in #79/#89 (`e26cd75`, `62d329e`); the runner was never migrated.

Why tests pass: `tests/test_recipes.py:600-740` patch `apps.recipes.services.runner.build_agent_graph` with `Mock()`, which accepts any kwargs.

### F3. The artifact "sandbox" is same-origin with the app — LLM code runs with full user authority — `LATENT / security / verified-by-trace (mechanism), strong-inference (exploit) / accidental`

- `frontend/src/components/ArtifactPanel/ArtifactPanel.tsx:192-194`: the iframe src is `/api/workspaces/<ws>/artifacts/<id>/sandbox/` — same origin as the app — with `sandbox="allow-scripts allow-same-origin allow-modals"`. `allow-scripts` + `allow-same-origin` on a same-origin document neutralizes the sandbox attribute entirely.
- `apps/artifacts/views.py:44-51`: CSP allows `'unsafe-eval'`, three CDNs, and `connect-src 'self'` — i.e. the artifact may fetch the Scout API.
- `config/settings/base.py:335-336`: `CSRF_COOKIE_HTTPONLY = False` — artifact JS in the same-origin frame can read the CSRF token and issue state-changing POSTs with the user's session cookie (sent automatically).
- `apps/artifacts/views.py:498-522` (`renderHTML`): re-creates and executes every `<script>` in agent-generated HTML, including remote `src`.

Artifact code is LLM-generated from materialized data; a prompt-injection payload in CommCare/Connect/OCS data (form text, chatbot messages — `raw_messages.content` is exactly attacker-writable territory) that steers the agent into emitting malicious artifact JS gets a foothold equal to the logged-in user: read any workspace data via `/api/...`, delete artifacts/knowledge, add members (if manager). Reachability requires the user to open the artifact, which the UI does eagerly in the artifact panel. The CLAUDE.md/docstring claim "sandboxed React rendering" does not match the implementation — that mismatch is itself a finding under the evidence standards. Fix shape: serve the sandbox from a separate origin (or drop `allow-same-origin` and pass data via postMessage), and tighten `connect-src`.

### F4. Two `teardown_schema` implementations with different invariants; the agent-reachable one corrupts state and hits sibling workspaces — `BROKEN-NOW (on use) / correctness (+data-loss for siblings) / verified-by-trace / accidental`

Worker task `apps/workspaces/tasks.py:609-663`: drops the schema, then marks the `TenantSchema` EXPIRED, flips data-bearing runs STALE, and fails dependent sibling `WorkspaceViewSchema` rows (`_fail_dependent_view_schemas`).

MCP tool `mcp_server/server.py:801-865` (`teardown_schema`, exposed to the LLM, `confirm=True` guard only): calls `mgr.ateardown(ts)` / `mgr.ateardown_view_schema(vs)` — which per `schema_manager.py:183-193` *only* perform the physical DROP, "callers are responsible for updating the model state" — and then **updates nothing**. Consequences after an agent-driven teardown:

- `TenantSchema` rows remain ACTIVE and `MaterializationRun` rows remain COMPLETED while the physical schema is gone; `get_schema_status` (server.py:689-735) keeps reporting `exists=True, state=active` with the stale table list from `run.result`.
- It iterates **all** `TenantSchema` rows for the workspace's tenants (`server.py:852-858`) — tenant schemas are shared, so a teardown requested in workspace A destroys the data under every other workspace on those tenants, without flipping their view-schema rows to FAILED (the worker task does; the tool doesn't).
- Only the live-table reconciliation in `pipeline_list_tables` (metadata.py:76-93, the #185 fix) saves `list_tables` from returning ghosts.

This is the same operation implemented twice with divergent semantics — and the two callables even share a name, which is how the divergence survives review.

### F5. Tenant/schema resolution ignores provider — cross-provider external-id collision routes one tenant to another's data — `LATENT / security / strong-inference / accidental`

Three mutually reinforcing holes, mechanisms all verified in code:

- `mcp_server/context.py:56-59` `load_tenant_context` filters `TenantSchema.objects.filter(tenant__external_id=tenant_id, state__in=[ACTIVE, MATERIALIZING])` — **no provider predicate**. `Tenant` is unique on `(provider, external_id)`, so `external_id` alone is ambiguous by design.
- `apps/workspaces/services/schema_manager.py:625-631` `_sanitize_schema_name` maps distinct external_ids to one name: Connect opp `123` and OCS bot `123` both → `t_123`; `my-project` and `my_project` → `my_project`.
- `schema_manager.py:66-77` `provision()` looks up the existing schema **by schema_name only** and returns it regardless of which tenant the row belongs to — so a Connect materialization can adopt (and write into) an OCS tenant's schema row and vice versa.

Connect opportunity IDs are numeric; OCS experiment IDs are stored as `str(exp["id"])` (`tenant_resolution.py:163`). If both providers ever yield the same numeral — plausible for small sequential IDs — workspace queries for one tenant read the other tenant's tables, and materializations interleave destructively (each writer `DROP TABLE … CASCADE`s shared table names). Severity is cross-tenant data exposure; likelihood depends on real ID spaces, hence LATENT/strong-inference. The fix is cheap: scope all three lookups by `(provider, external_id)` and prefix schema names per provider.

### F6. Workspace roles are unenforced for most content writes — `LATENT / security / verified-by-trace / accidental`

The `read` role is enforced in exactly four places (data-dictionary annotate `workspaces/api/views.py:500`, refresh `views.py:330`, workspace management `workspace_views.py`, transformations `transformations/views.py:84,154`). Everywhere else, plain membership suffices:

- Artifacts: PATCH/DELETE/undelete — `apps/artifacts/views.py:893-932`, no role check; a `read` member can rename, soft-delete (and undelete) any artifact.
- Knowledge: create/update/delete — `apps/knowledge/api/views.py:115,189,212,271`, no role check.
- Recipes: create/update/delete/run/share — `apps/recipes/api/views.py` throughout, no role check; `read` members can flip `is_public` on runs (creating unauthenticated share links).
- Chat/materialization: any member can chat, which can trigger materialization — arguably intended, but undocumented.

Either the role model is enforced uniformly via a shared permission helper, or `read` should be removed from the UI; today it communicates a guarantee the API does not provide.

### F7. Artifacts: two data mechanisms, and the live one is wired to single-tenant assumptions — `BROKEN-NOW (multi-tenant live artifacts) + DEBT (the split itself) / correctness / verified-by-trace / mixed`

- The split: static `data` vs live `source_queries` (`apps/artifacts/models.py:92-119`; sandbox JS branches on `has_live_queries`, `views.py:251-275`). The agent prompt now mandates live queries (`artifact_tool.py:88-92`), so static artifacts are the evolutionary remnant — but both render paths, the export path, and the Data tab all still carry dual logic.
- Multi-tenant breakage: `ArtifactQueryDataView` (`apps/artifacts/views.py:795-800`) resolves `tenant = workspace.tenants.afirst()` then `load_tenant_context(tenant.external_id)`. In a multi-tenant workspace the agent writes SQL against `prefix__table` view names that exist only in the `ws_*` schema; executed against the first tenant's schema they fail ("relation does not exist"), so every live artifact in a multi-tenant workspace renders an error. The chat agent and this view answer "which schema does this workspace query?" differently — the fourth distinct resolver in the codebase (see CC1).
- No migration story for stored SQL: `source_queries` embed physical table names; teardown/re-materialization renames or drops them and nothing detects or repairs affected artifacts (the original incident in the review brief). Same applies to `TableKnowledge.table_name`, which embeds `schema.table` qualified names (`workspaces/api/views.py:289`) that go stale on every schema swap.

### F8. Artifacts are never linked to their thread — public share pages always show zero artifacts — `BROKEN-NOW / correctness / verified-by-trace / accidental`

`create_artifact_tools(workspace, user)` is called without `conversation_id` (`apps/agents/graph/base.py:694`; factory default `None` → stored as `""`, `artifact_tool.py:57,197`). The only consumers of the linkage are `_get_thread_artifacts` (`apps/chat/thread_views.py:52-68`) and the public thread endpoint (`thread_views.py:245`), which filter `conversation_id=str(thread_id)` — guaranteed empty. The `thread_id` is available in `AgentState`; the factory just predates state injection and was never updated. (In-conversation artifact display works via the live tool-result stream, which masks the breakage until someone opens a shared thread.)

### F9. The OAuth-token transport layer is dead code with a false docstring — `DEBT / velocity / verified-by-trace / accidental`

`chat_view` fetches `oauth_tokens` and threads them into `build_agent_graph(...)` and `config` (`apps/chat/views.py:162,172,196`); `build_agent_graph` accepts the parameter and never reads it (`graph/base.py:485` — only the signature and docstring mention it); `mcp_server/auth.py:13` `extract_oauth_tokens` has **zero callers** and claims "Tokens are injected by the Django chat view at the transport layer" — no such injection exists anywhere. Credentials are actually resolved worker-side from the DB (`aresolve_credential`). Also in this family: `QueryContext.oauth_tokens`/`TenantContext` (`mcp_server/context.py:34-44`, TenantContext has no constructor callers), `get_user_oauth_tokens` covering only CommCare providers (`mcp_client.py:79-90`). ~5 files of plumbing that misleads every reader (and at least one v1 reviewer) about where credentials flow.

### F10. PNG/PDF export: service fully implemented, endpoint returns 501 — `DEBT / velocity / verified-by-trace / accidental`

`ArtifactExporter.export_png/export_pdf` (playwright, `apps/artifacts/services/export.py:373-454`) have no callers; the view hard-returns 501 for both formats (`apps/artifacts/views.py:979-987`) with an error message pointing at the *same URL* the client just called. Only HTML export works. ~200 lines of dead, dependency-bearing code; classic 80%-feature.

### F11. System-prompt assembly is expensive and unbounded — `DEBT / cost-perf / verified-by-trace / mixed`

Per cache-miss (60s TTL per workspace+user, `graph/base.py:127-142`): `_fetch_schema_context` calls `pipeline_describe_table` for every table (`base.py:267-271`), and each call opens a **fresh psycopg connection** to the managed DB (`metadata.py:199` → `query.py:68-93` `_execute_async_parameterized` connects per call; `_live_tables_in_schema` adds another). Ten tables ⇒ ~12 connections per prompt build. `KnowledgeRetriever.retrieve()` (`knowledge/services/retriever.py`) injects **all** KnowledgeEntries and TableKnowledge rows with no cap or relevance filter (only learnings are capped at 20); its `user_question` parameter is unused — it is a dump, not a retriever — so prompt size and cost grow linearly with knowledge accumulation, forever, on every turn. Cosmetic but telling: the retriever emits its own `## Knowledge Base` heading inside the `## Knowledge Base` section `_build_system_prompt` wraps it in (`base.py:733` + `retriever.py:62`).

### F12. The FutureApp `current_app` bug was fixed where it bit, not where it lives — `LATENT / correctness / strong-inference / accidental`

`tasks.py:693-712` documents the failure (module-level `from procrastinate... import current_app` binds the unresolved `FutureApp` Blueprint whose `job_manager` raises `AttributeError`) and fixes it for the janitor by reading job status via the ORM. But two sibling sites still hold module-level `current_app` imports used for cancel/abort: `mcp_server/server.py:32→633` (rollback when ThreadJob creation fails) and `apps/workspaces/api/materialization_views.py:9→109,205` (orphan-run abort, retry rollback). All are wrapped in `try/suppress`, so if the same binding problem occurs in those processes the aborts silently no-op — exactly the failure-shape the janitor had. Whether the API/MCP import orders resolve the app in time is environment-dependent; the inconsistency is the finding.

### F13. Prompt ↔ tool contract drift: the agent is told to pass a parameter the tool doesn't accept — `DEBT / correctness / strong-inference / accidental`

`_fetch_schema_context` instructs: *"Call `run_materialization` with `pipeline="<name>"`"* (`graph/base.py:222-227`), but the MCP tool signature has no `pipeline` parameter (`mcp_server/server.py:521-527` — pipeline selection moved into `materialize_workspace` per-provider). Depending on FastMCP's handling of unexpected args, the agent's first materialization attempt in a fresh workspace either errors-and-retries (burning a turn and feeding the escalation counter) or silently drops the arg. Same drift family as F2, caught only because the prompt text is greppable.

### F14. Three naming strata and their residue — `DEBT / velocity / verified-by-trace / accidental`

The project→tenant/domain→workspace migrations (#89 `62d329e`, #142 `abd64e4`, TenantCredential→TenantConnection 2026-06-05) each left a stratum:

- Frontend: the entire workspace concept is "domains" (`store/domainSlice.ts` — `activeDomainId`, 140 references; `TenantMembership` aliased to `WorkspaceListItem` with fake legacy fields "so code referencing these doesn't break at compile time").
- Backend: `envelope.py:project_id`; `stream.py:182` audit-logs `input_state.get("project_id")` (never set — always empty, so the audit trail's workspace attribution is blank); artifact view docstrings say "project membership"; `Workspace.data_dictionary` "legacy fields retained" + single-tenant compat shims (`models.py:131-167`); `schema_manager.py:174` "Celery task".
- Content pages (`/artifacts`, `/recipes`, `/knowledge`, `/data-dictionary`) are not workspace-scoped in the URL — they key off the global `activeDomainId`, the design that produced the cross-workspace threadId incident (fixed for threads in `bdr/stale-thread-handling`, but the pattern remains for the other four pages: a stale global selection silently shows the wrong workspace's content list until the next fetch).

### F15. Account model: password-first accounts permanently orphaned from later OAuth identity — `DEBT / correctness / verified-by-trace / essential-leaning-mixed`

Mechanism (matches the known symptom): password signup under `ACCOUNT_EMAIL_VERIFICATION = "optional"` (`base.py:199`) creates an unverified `EmailAddress` (and prod has no visible email-sending config — `EMAIL_BACKEND` only set in development.py). Both reconciliation gates require verification: allauth's `SOCIALACCOUNT_EMAIL_AUTHENTICATION` connect path, and Scout's own `reconcile_existing_user_on_login` which explicitly refuses to merge when the canonical user lacks a *verified* EmailAddress (`apps/users/signals.py:104-115`). So password-then-OAuth users get two accounts forever; the only remedy is the operator command (`merge_duplicate_users`). The refusal is correct security posture (unverified email must not grant account takeover); the incoherence is shipping a password signup flow whose accounts can never verify. Either require/send verification, or remove password signup, or surface a verified-email flow before OAuth linking.

### F16. MCP server trusts its network completely — `LATENT / security / verified-by-trace (code), reachability deployment-dependent / mixed`

All MCP tools execute for any caller who supplies a `workspace_id` — there is no caller authentication; `mcp_server/auth.py` (see F9) is dead, and the only protection is transport-level DNS-rebinding/host allowlisting (`server.py:909-912`) plus Docker network non-exposure (`docker-compose.yml` uses `expose`, Kamal keeps it on the internal network). Within that network boundary, anything (an SSRF in the API, a compromised container, a developer port-forward) gets: arbitrary cross-workspace reads (`query`, `list_tables` for any workspace_id) and destructive teardown (F4). The per-schema read-only roles limit SQL writes, but not reads of other tenants (each call is given the right role for whatever workspace it names). Defense-in-depth would put a shared-secret header or mTLS between API/worker and MCP.

### F17. Provider payload assumptions baked in untested (same class as the 63-byte truncation bug) — `LATENT / correctness / hypothesis→strong-inference / accidental`

- `Tenant.canonical_name = CharField(max_255)` filled directly from provider responses (`tenant_resolution.py:60-64,107-111,160-164`): a Connect opportunity / OCS experiment name >255 chars raises `DataError` mid-resolution; the caller catches broadly and logs a warning (`signals.py:64-78`), so the user simply sees missing opportunities with no error. (The view-name truncation incident was the same genus; the fix bounded view prefixes but not this inbound field.)
- `User.email` from `extra_data` is taken as-is in `reconcile_existing_user_on_login` (`signals.py:88-101`) — no length/format guard before `User.save`.
- Connect writers assume integer cursor ids (`materializer.py:1237-1247` `_max_id` skips non-ints silently — a provider switch to string ids would quietly disable resume rather than fail).

### F18. Deleting threads/members leaks checkpointer data — `DEBT / correctness (data retention) / verified-by-trace / accidental`

`WorkspaceMemberDetailView.delete` removes the member's `Thread` rows (`workspace_views.py:517`), and thread deletion elsewhere is row-level too — but nothing ever deletes the corresponding LangGraph checkpoints (`AsyncPostgresSaver` tables), so full conversation contents (including query results) survive membership removal and thread deletion indefinitely, keyed by a thread UUID that the `public_thread_view` would happily serve if a Thread row with `is_shared` ever pointed at it again. Unbounded growth + retention-policy gap.

### F19. Stream timeout cannot fire while an event hangs — `LATENT / cost-perf / verified-by-trace / accidental`

`langgraph_to_ui_stream` checks the deadline only *between* events (`stream.py:104-110`); `await event_stream.__anext__()` itself is unbounded, so a hung tool/LLM call holds the SSE response (and its DB/app resources) forever from this layer — the 300s "timeout" only triggers if events keep arriving slowly. (The resume path got a real `asyncio.wait_for`; the interactive path didn't — another fixed-where-it-bit instance.)

## 4. Cross-cutting patterns

**CC1 — "Which schema does this workspace query?" is answered five ways.** (1) `load_workspace_context` (MCP/chat: tenant-count branch, ACTIVE view schema required); (2) `_resolve_tenant_schema` in the data-dictionary API (first ACTIVE/MATERIALIZING TenantSchema, no multi-tenant handling at all — the data dictionary silently shows tenant #1 for multi-tenant workspaces); (3) `ArtifactQueryDataView` (`tenants.afirst()` always); (4) `RefreshSchemaView`/`refresh_tenant_schema` (its own `_r` schema scheme); (5) `_fetch_schema_context` vs `_fetch_multi_tenant_schema_context` in the prompt builder. Findings F1, F7, and the data-dictionary multi-tenant gap are all instances of resolvers 2–4 not tracking resolver 1. One shared `resolve_query_target(workspace) -> (schema_name, kind)` would collapse the class.

**CC2 — Contract drift between modules is undetectable.** `build_agent_graph` changed shape and recipes kept compiling (Python kwargs + mocked tests, F2); `run_pipeline` grew `provision()` and refresh kept "working" until 30 minutes later (F1); the prompt references tool parameters that no longer exist (F13); `conversation_id` consumers outlived the producer (F8). The common factor: the test suite mocks every one of these seams (`Mock()` for the graph, mocked resolvers, mocked MCP), so only schema-shaped checks (a contract test that calls the real factory signatures, a CI grep that prompt-referenced tool args exist) would catch the next one.

**CC3 — Fixed where it bit, siblings left behind.** FutureApp `current_app` (fixed in tasks.py, alive in server.py & materialization_views.py — F12); timeout hardening (resume path yes, interactive stream no — F19); state-row updates on teardown (task yes, MCP tool no — F4); TTL touch (chat touches schemas, artifact query-data touches only the tenant schema, not the view schema — `artifacts/views.py:810-812`); identifier-length guards (view prefixes bounded, inbound canonical_name not — F17). A "list every sibling site" step in incident fixes would prevent this; the git history shows the team is *excellent* at fixing the bitten site.

**CC4 — Role/permission checks are per-endpoint copy-paste, with omissions as the failure mode** (F6). Membership resolution is nicely centralized (`workspace_resolver.py`, three sync/async variants), but *role* enforcement is each view's responsibility and most content views skipped it.

**CC5 — Naming strata as drag** (F14): every reader must hold project≈domain≈workspace and tenant≈domain≈chatbot≈opportunity equivalences in their head; the frontend aliases types to keep dead names compiling. This is pure accidental complexity and it measurably misleads (audit logs log an empty `project_id`).

**CC6 — Where complexity is essential, the code is good.** The CAS state machines in `materializer.py`/`tasks.py`, the shared-tenant-schema cascade handling, the janitor/poll-backstop double coverage, and the resume-claim protocol are intricate because the problem is; the comments there describe real invariants and match the code. This is the inverse of the satellites, where simple features hide broken wiring.

## 5. Recommendations (prioritized)

**Now (days, stops active damage):**
1. Disable or fix the refresh endpoint (F1). Minimal fix: have `refresh_tenant_schema` drop the `_r` indirection entirely and just call `run_pipeline` (provision() already handles refresh-in-place semantics), deleting `create_refresh_schema`; or pass an explicit target schema through `run_pipeline`. Add a test that asserts the schema containing the loaded rows is the one left ACTIVE.
2. Fix or feature-flag recipe runs (F2): `build_agent_graph(workspace=recipe.workspace, mcp_tools=await get_mcp_tools(), …)` + current state keys; add a RecipeRun janitor or create the row after the graph builds.
3. Make the MCP `teardown_schema` tool delegate to the worker task (or call the same state-updating helper), and scope it to the workspace's *exclusive* tenants or require explicit per-tenant confirmation (F4).
4. Pass `thread_id` into `create_artifact_tools` (F8) — one-line producer fix.

**Next (1–2 weeks, closes the security gaps):**
5. Re-sandbox artifacts (F3): separate sandbox origin or drop `allow-same-origin` + postMessage data transport; remove script re-execution from `renderHTML`; restrict `connect-src`.
6. Centralize role enforcement (F6): one `require_role(membership, WorkspaceRole.X)` helper, applied to every non-GET content endpoint; a test that walks the URLconf and asserts each write endpoint either checks a role or is on an explicit allowlist.
7. Scope tenant/schema resolution by provider and prefix schema names per provider (F5). Add a data migration check for existing collisions.
8. Shared secret between API/worker and MCP server (F16); delete the dead oauth-token plumbing while in there (F9).

**Then (structural, ordered by what it unblocks):**
9. Introduce the single workspace→query-target resolver (CC1) and migrate the data-dictionary API, artifact query-data, and prompt builders onto it. This unblocks honest multi-tenant support in the data dictionary and live artifacts (F7).
10. Contract tests for the drift-prone seams (CC2): instantiate real tool factories and the real graph builder in tests (no `Mock()` for signatures); assert prompt-referenced tool names/args against the live MCP tool schemas.
11. Stored-SQL migration story (F7/known symptom): record the schema "generation" on artifacts/TableKnowledge at creation; on schema swap, mark dependents stale and surface a re-validate affordance instead of silent breakage.
12. Naming cleanup (F14): rename `domainSlice`→`workspaceSlice` (mechanical, 140 refs), drop `project_id` from envelope/audit or populate it, delete `Workspace` legacy fields after verifying the JSONField fallback in `TableDetailView` (`workspaces/api/views.py:425-427`) is dead.
13. Knowledge dump → actual retrieval or caps (F11); pool managed-DB connections for metadata queries.
14. Decide the account story (F15): either wire verification email in prod + verify-then-merge UX, or remove password signup.
15. Incident-fix checklist addition (CC3): "list sibling sites of the fixed pattern" as a PR template item — the git history shows this single habit would have prevented F4, F12, F19.

## 6. What's actually fine

- **`materializer.py` run state machine** — CAS-guarded phase transitions, per-source commit isolation, resumable cursors with honest `in_progress` states, cancellation semantics that preserve committed work. Comments match behavior (verified at every CAS site).
- **`tasks.py` job lifecycle & janitors** — claim-by-CAS resume, status-unknown-means-don't-touch reconciliation, API-side backstop for a sick worker, synthetic failure messages. Incident-hardened and it shows.
- **`SchemaManager.build_view_schema`** — collision/length checks on final names *before* DDL, FAILED-state + last_error persistence, idempotent rebuild. The 63-byte incident fix is thorough at this site.
- **Chat entry path** — thread-ownership validation with non-leaking 404s, dangling-tool-call repair on both entry and resume, CSRF + rate limiting on a raw async view, stale/foreign-thread recovery semantics.
- **SQL safety layering** — sqlglot validation (statement type, dangerous functions, schema allowlist, limit injection) *plus* per-schema read-only Postgres roles; either alone would be worrying, together they're solid.
- **Workspace member management** — uniform manage-role checks, last-manager guards, tenant-access precondition for invites.
- **`merge_users`** — explicit conflict policy per relation, long-tail FK discovery via `_meta`, single transaction, dry-run mode.
- **Procrastinate `task` decorator** (`config/procrastinate.py`) — connection hygiene with a test that enforces every task uses it, and an upstream-tracking removal note.
- **Pipeline registry** — declarative YAML with sane provider routing; adding a provider is genuinely additive.
- **Credential model (TenantConnection)** — fail-closed OCS team mismatch check, Fernet at rest for tokens and API keys, archive-on-remove.

## 7. Coverage appendix

**Deep-read (line-by-line):** `apps/workspaces/` (models, tasks, schema_manager, workspace_service, workspace_resolver, api/views, api/workspace_views, api/jobs_views, api/materialization_views, urls); `mcp_server/` (server, context, auth, envelope, pipeline_registry, services/metadata, services/query, services/sql_validator, services/materializer lines 1–1510); `apps/agents/` (graph/base, tools/artifact_tool, mcp_client); `apps/chat/` (views, models, thread_views, stream, checkpointer, helpers, rate_limiting); `apps/users/` (models, signals, adapters, apps, views, services/merge, services/credential_resolver, services/tenant_resolution); `apps/artifacts/` (models, views, urls); `apps/recipes/` (models, services/runner, api/views); `apps/knowledge/` (models, services/retriever); `config/` (urls, settings/base, settings/production, settings/development, procrastinate); frontend: `router.tsx`, `store/domainSlice.ts`; `tests/test_recipes.py` (runner section).

**Skimmed:** `apps/agents/tools/recipe_tool.py` (first 120 lines), `apps/artifacts/services/export.py` (head + structure), frontend `dictionarySlice`/`recipeSlice`/`ArtifactPanel` (targeted greps), `pipelines/*.yml` (names only), docker-compose/deploy ymls (port/URL greps), git log (subjects, churn counts, targeted `-S` searches).

**Not examined (honest gaps for the gap loop):** `mcp_server/loaders/*` (all 20 loader files — pagination/retry/payload-shape logic unreviewed); `mcp_server/services/dbt_runner.py` and the entire transformations app internals (`executor.py`, `lineage.py`, `commcare_staging.py`, `models.py`, `views.py` beyond a role-check grep); `apps/users/auth_views.py`, `providers/*` (custom OAuth provider implementations), `decorators.py`, `services/token_refresh.py`, `services/ocs_team.py`, `api_key_providers/*`; `apps/agents/` prompts content, `memory/checkpointer.py`, `tracing.py`, `graph/state.py`; `apps/chat/message_converter.py`, `constants.py`; `apps/knowledge/api/views.py` body + `utils.py` + learning_tool.py; `apps/recipes/api/serializers.py`, admin modules everywhere; migrations (all apps — squash/rename history unreviewed); `config/middleware/embed.py`, `taskbadger.py`, `asgi/wsgi`, `views.py` (widget_js); ~95% of the frontend (all pages/components incl. WorkspaceDetailPage, WorkspaceSwitcher, ChatPanel/ChatMessage, EmbedPage, contexts, api/ clients, uiSlice/artifactSlice/knowledgeSlice/authSlice); the test suite beyond test_recipes (what the other ~70 test files mock is unassessed); `tests/qa/`, `scripts/`, `infra/`, `Dockerfile*`, `docker-entrypoint.sh`, Kamal configs beyond grep; `THURS_TEST_PLAN.md`/`TODO.md`/existing review docs (deliberately unread to stay independent — note `ARCHITECTURE_REVIEW.md` and `docs/architecture-review-2026-06-12.md` exist at repo root and were NOT consulted).
