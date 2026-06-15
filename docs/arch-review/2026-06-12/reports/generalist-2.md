# Architecture Review — Generalist 2

Reviewer: generalist-2 (independent full-codebase pass, no cartography map, no other reviewers' output).
Date: 2026-06-12. Report only; no code changed.

Evidence standards followed: BROKEN-NOW claims carry a full entry-point → consequence chain with file:line; comments treated as claims, not facts; confidence labeled per finding; reachability checked; essential vs accidental complexity labeled.

---

## 1. Executive summary

Scout's backbone — workspace-scoped access control, the SQL safety layer, the materialization state machine's CAS discipline, and the user-merge service — is in better shape than the "rapid AI-generated codebase" framing suggests. Those areas show real defensive engineering (sqlglot validation + `SET ROLE` read-only execution; conditional-UPDATE state transitions; carefully ordered teardown bookkeeping).

But the system has a characteristic failure shape: **the core path is hardened by repeated incident-driven fixes, while every secondary path that shares its infrastructure has silently drifted or broken.** Three concrete examples, all traced end-to-end in this review:

1. **The Data Dictionary "Refresh data" button destroys the tenant's data.** The refresh task creates a new `_r<hex>` schema, but the pipeline it invokes resolves its *own* schema by base name and loads the data there; the task then activates the empty new schema and schedules teardown of the schema that just received the fresh data. Every press of that button ends, 30 minutes later, with an empty ACTIVE schema and the data dropped. The test for this task mocks `run_pipeline` outright, so the suite cannot see it.
2. **Recipe execution is 100% broken.** The runner calls `build_agent_graph(tenant_membership=...)` — a parameter that no longer exists after the projects→workspaces refactor. Every "Run recipe" click returns HTTP 500 and strands a `RecipeRun` in RUNNING. The feature was migrated at the model/API layer but not at the agent-graph seam.
3. **Live-query artifacts ignore the multi-tenant routing layer.** The artifact query endpoint resolves `workspace.tenants.first()` and queries that tenant's schema directly, while the agent that *wrote* those queries was operating against the `ws_*` view schema with namespaced views. Multi-tenant live artifacts cannot work.

The pattern behind all three is the same: a cross-cutting change (workspace model, multi-tenant view schemas, the procrastinate pipeline) was driven to completion on the chat/materialization happy path and never propagated to the consumers that embed references to the old world (refresh endpoint, recipes, artifacts). There is no mechanism — neither a shared resolver that all consumers must go through, nor integration tests at the seams — that would catch this class of drift. The heavily-mocked test suite actively hides it.

Secondary themes: the same problem is solved 3–4 different ways in several places (cancellation ×3, table-catalog listing ×4, workspace resolution ×4 variants — the resolvers at least agree); role-based authorization exists only on a subset of DRF endpoints while the agent/MCP path enforces no roles at all (a read-only member can drop all workspace data via chat); and the artifact sandbox's `allow-scripts allow-same-origin` iframe neutralizes its own sandbox on an authenticated origin.

The materialization/job subsystem (tasks.py, materializer.py, the ThreadJob janitor) is the most incident-hardened code in the repo and also the most accreted: it works, but it is a distributed transaction emulated by compensations (sleep-retry hedges, janitors, API-side backstops, CAS state flips) layered across four processes, with every layer annotated by the postmortem that produced it. That complexity is partly essential (background jobs + chat resume genuinely is hard) and partly accidental (the ThreadJob↔procrastinate↔MaterializationRun linkage was never designed as one model).

---

## 2. As-built architecture map

**Processes** (4): Django API (ASGI), MCP server (FastMCP, separate process, *same Django ORM and codebase*), procrastinate worker (same codebase), Vite/React frontend. Plus two PostgreSQL databases: the platform DB (Django models, procrastinate queue, LangGraph checkpoints) and the managed DB (per-tenant `t_*`/sanitized schemas, per-workspace `ws_*` view schemas, per-schema `_ro` roles).

**Data flow, chat turn:** frontend → `POST /api/chat/` (`apps/chat/views.py`) → workspace membership check → `build_agent_graph` (`apps/agents/graph/base.py`) which *in-process* pre-fetches the schema catalog for the system prompt → LangGraph loop → MCP tools over HTTP (`mcp_server/server.py`) → `load_workspace_context` (`mcp_server/context.py`) routes single-tenant→tenant schema, multi-tenant→view schema → `execute_query` under `SET ROLE <schema>_ro` (`mcp_server/services/query.py`).

**Materialization:** agent calls MCP `run_materialization` → defers `materialize_workspace` (procrastinate) + creates a `ThreadJob` row binding job↔thread → worker runs `run_pipeline` per tenant (`mcp_server/services/materializer.py`: provision → discover → load per source with per-page progress/cancellation → transform via dbt assets) → rebuilds the workspace view schema and sibling workspaces' view schemas → defers `resume_thread_after_materialization`, which re-invokes the agent with a system-framed summary. Janitors (`expire_stale_thread_jobs`, every 15 min) and an API-side poll backstop reconcile stuck jobs; `expire_inactive_schemas` (every 30 min) TTLs idle schemas.

**Identity/tenancy:** allauth with three custom OAuth providers (CommCare, Connect, OCS) + Google/GitHub. Post-login tenant resolution (`apps/users/services/tenant_resolution.py`) upserts `Tenant`/`TenantMembership`/`TenantConnection`; a `post_save` signal auto-creates a MANAGE workspace per (user, tenant). `Workspace` ↔ `Tenant` is M2M; multi-tenant workspaces get a `WorkspaceViewSchema` of UNION-ALL-ish namespaced views. Credentials resolve per membership via `credential_resolver` (OAuth token or Fernet-encrypted API key), fail-closed on OCS team mismatch.

**Artifacts:** agent tools create `Artifact` rows (static `data` and/or `source_queries`); rendered in a same-origin sandbox page (`apps/artifacts/views.py`) inside an iframe; live queries re-executed at render time by `ArtifactQueryDataView`.

**Knowledge/recipes:** `KnowledgeRetriever` injects metrics/rules/learnings into the system prompt; recipes store a prompt template replayed through a freshly built agent graph (currently broken, see F2).

---

## 3. Findings

### F1. Data Dictionary "Refresh" loads data into the wrong schema, then destroys it — BROKEN-NOW / data-loss

**Confidence: verified-by-trace. Complexity: accidental. Reachable: yes (Data Dictionary page button).**

Chain:
- Entry: `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx:36` → `refreshSchema()` → `frontend/src/store/dictionarySlice.ts:197` `POST /api/workspaces/<id>/refresh/`.
- `RefreshSchemaView.post` (`apps/workspaces/api/views.py:352-365`) calls `SchemaManager().create_refresh_schema(tenant)` which creates a TenantSchema named `{base}_r{8hex}` (`apps/workspaces/services/schema_manager.py:169-181`), then defers `refresh_tenant_schema`.
- `refresh_tenant_schema` (`apps/workspaces/tasks.py:126`) physically creates the `_r` schema (tasks.py:150) and then calls `run_pipeline(membership, credential, pipeline_config)` (tasks.py:173) — **passing no schema**.
- `run_pipeline` resolves its own target: `tenant_schema = SchemaManager().provision(tenant_membership.tenant)` (`mcp_server/services/materializer.py:183`), and `provision()` looks up by the *base* sanitized name `_sanitize_schema_name(tenant.external_id)` (`schema_manager.py:66-78`) — which never equals the `_r<hex>` name. All data is loaded into the existing (or resurrected) base schema.
- Back in the task: the empty `_r` schema is marked ACTIVE (tasks.py:182-184), and **every other ACTIVE schema for the tenant — including the base schema that just received the fresh data — is flipped to TEARDOWN and scheduled for `DROP SCHEMA ... CASCADE` in 30 minutes** (tasks.py:188-197). Teardown also flips the runs to STALE and fails dependent view schemas (tasks.py:639-653).

Consequence: after every refresh, the tenant's only ACTIVE schema is empty (no MaterializationRun rows attach to it, so the catalog and data dictionary show nothing), and 30 minutes later the real data is physically dropped. Recovery requires a fresh materialization. Multi-tenant siblings' view schemas get flipped to FAILED as collateral.

Why tests don't catch it: `tests/test_refresh_task.py:73,109` patch `apps.workspaces.tasks.run_pipeline` with a bare `MagicMock` — the test never observes which schema receives data.

Note: this is an evolutionary remnant — `create_refresh_schema`'s docstring still says "dispatching the Celery task", and the whole blue/green refresh design predates `run_pipeline`'s own `provision()` call. Two generations of design occupy the same path.

### F2. Recipe execution is fully broken (signature drift from the workspace refactor) — BROKEN-NOW / correctness

**Confidence: verified-by-trace. Complexity: accidental (rename residue). Reachable: yes (Recipes page "Run").**

Chain:
- Entry: `frontend/src/store/recipeSlice.ts:135` `POST /api/workspaces/<id>/recipes/<id>/run/` → `RecipeRunView.post` (`apps/recipes/api/views.py:89-108`) → `RecipeRunner.execute()`.
- `execute()` creates the `RecipeRun` row (status RUNNING) then calls `_build_graph()` **outside** the inner try/except.
- `_build_graph` calls `build_agent_graph(tenant_membership=self._tenant_membership, user=..., checkpointer=None)` (`apps/recipes/services/runner.py:115-118`).
- `build_agent_graph(workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)` (`apps/agents/graph/base.py:480-486`) has no `tenant_membership` parameter → `TypeError` on every call.
- The view's outer `except Exception` returns HTTP 500; the RecipeRun row is left in RUNNING forever (no janitor exists for RecipeRuns).

Even if the call were fixed, the runner is two refactors behind: it builds `initial_state` with `tenant_id` / `tenant_name` / `tenant_membership_id` keys (pre-workspace AgentState shape; current state uses `workspace_id`), passes `mcp_tools=None` (agent would have no data tools), and the "save-recipe" slash command + public recipe pages still actively advertise the feature. This is the prompt's "80% feature" archetype: model + CRUD + UI complete, execution seam dead.

### F3. Artifact sandbox: `allow-scripts allow-same-origin` on an authenticated same-origin page — LATENT / security

**Confidence: verified-by-trace (configuration); exploit impact strong-inference. Complexity: accidental.**

- The sandbox document is served same-origin, session-authenticated (`ArtifactSandboxView`, `apps/artifacts/views.py:673-717`), and executes agent-generated code via Babel + `new Function` (views.py:382, 563, 605); HTML artifacts re-attach and execute their `<script>` tags verbatim (views.py:512-522).
- The parent embeds it with `sandbox="allow-scripts allow-same-origin allow-modals"` (`frontend/src/components/ArtifactPanel/ArtifactPanel.tsx:194`). Per the HTML spec, `allow-scripts` + `allow-same-origin` on a same-origin document makes the sandbox attribute a no-op: the framed code is same-origin with the app and can reach `window.parent`'s DOM, cookies-bearing fetch, etc.
- The page CSP allows `'unsafe-eval'` and `connect-src 'self'` — i.e., artifact code may issue credentialed requests to every `/api/` endpoint as the *viewing* user.

Consequence: an artifact is stored, agent-generated code; its code can be influenced by prompt injection from materialized third-party data (CommCare form contents, Connect names, OCS chat messages are all attacker-writable upstream) or by any read_write member. Any other member who opens the artifact executes that code with their session — stored XSS with full API reach (including MANAGE-only endpoints if the viewer is a manager). The CSP work (nonces, `default-src 'none'`) shows intent to sandbox, but the iframe attributes and same-origin serving defeat it. Defensive fix direction: serve the sandbox from a separate origin or drop `allow-same-origin`, and proxy data in via postMessage (the code already has a postMessage channel).

### F4. No role enforcement on the agent path: read-only members can destroy workspace data — BROKEN-NOW / security

**Confidence: verified-by-trace. Complexity: accidental (role layer added later, only to DRF endpoints).**

Chain:
- `chat_view` requires only *any* WorkspaceMembership (`apps/chat/views.py:109-114`, via `_resolve_workspace_and_membership`, `apps/chat/helpers.py:88-120`); `user_role` is hardcoded `"analyst"` (views.py:213).
- The agent's toolset includes `teardown_schema` (`apps/agents/graph/base.py:65-76`), and `workspace_id` is injected server-side from state.
- MCP `teardown_schema` (`mcp_server/server.py:801-865`) checks only `confirm=True` and workspace existence — no membership at all, no role — then physically drops the workspace view schema and **every TenantSchema of every tenant in the workspace** (shared across other workspaces).

So a `read`-role member (or any member, in a workspace whose data other members rely on) can ask the agent to tear down all materialized data. Meanwhile the equivalent REST surface (`RefreshSchemaView`, table annotations) carefully requires READ_WRITE/MANAGE (`apps/workspaces/api/views.py:330`, :500). `run_materialization` via MCP similarly checks membership but not role, while the REST retry endpoint mirrors it. The `WorkspaceRole` model and `permissions.py` classes exist and are correct — they are simply not wired into the chat/MCP trust boundary, which is the primary way users touch the system. (`backfill_readonly_roles` management command suggests roles were retrofitted.)

### F5. MCP `teardown_schema` drops physical schemas without state bookkeeping — LATENT / correctness

**Confidence: verified-by-trace. Complexity: accidental (duplicate implementation).**

The worker task `teardown_schema` (`apps/workspaces/tasks.py:609-663`) does the full dance: flip record EXPIRED, flip runs STALE, fail dependent sibling view schemas. The MCP tool of the same name (`mcp_server/server.py:836-858`) calls `mgr.ateardown(ts)` / `ateardown_view_schema(vs)` — physical `DROP SCHEMA` only — and **never updates `TenantSchema.state`, `WorkspaceViewSchema.state`, or the runs**. After an agent-driven teardown:
- `TenantSchema` rows stay ACTIVE while the physical schema is gone (`SchemaManager.teardown` docstring explicitly says "callers are responsible for updating the model state" — this caller doesn't).
- `get_schema_status` reports `exists: true, state: active` with the stale `tables` list read from `run.result` (server.py:715-724).
- Sibling workspaces sharing those tenants keep ACTIVE-but-empty view schemas (the views were cascade-dropped) — the exact ghost condition the worker task's `_fail_dependent_view_schemas` was built to prevent.

The only reason this mostly self-heals is that `pipeline_list_tables` reconciles against `information_schema` (issue #185 fix) and `provision()` recreates missing physical schemas. Two implementations of "teardown", one with the invariants and one without — same anti-pattern as F1.

### F6. Three cancellation mechanisms; the MCP one doesn't actually cancel — DEBT / correctness

**Confidence: verified-by-trace. Complexity: accidental.**

- API path (`materialization_cancel_view`, `apps/workspaces/api/materialization_views.py:22-121`): sets runs CANCELLED *before* signalling procrastinate, with explicit tracked/orphan/other-user case analysis — correct but a monument of special-casing.
- Worker checkpoint: `_run_pipeline_with_progress.updater` raises only when `state == CANCELLED` (`apps/workspaces/tasks.py:491-494`).
- MCP `cancel_materialization` (`mcp_server/server.py:478-482`) sets the run to **FAILED** (with `result.cancelled=True`) and never aborts the procrastinate job. The mid-LOAD checkpoint therefore never fires; the pipeline keeps loading pages until the next phase-transition CAS misses. The tool's docstring ("Cancel a running materialization pipeline") and its behavior diverge; downstream, `_aggregate_materialization_state` classifies the run as "failed" rather than "cancelled", producing the wrong resume message. There is also `cancel_job_view` (jobs endpoint) as a third entry point. Three doors, two semantics.

### F7. Tenant schema namespace: no provider prefix, no 63-byte bound, provision() doesn't verify ownership — LATENT / data-leak class

**Confidence: strong-inference. Complexity: accidental. Reachability: requires colliding external_ids — plausible across providers.**

- `_sanitize_schema_name(tenant.external_id)` (`schema_manager.py:625-631`) ignores the provider. `Tenant` is unique on (provider, external_id) (`apps/users/models.py:124`), but the physical namespace is keyed on external_id alone: Connect opportunity `123` → `t_123`; a CommCare domain literally named `123` or `t_123` → also `t_123`.
- `provision()` resolves by `schema_name` **without filtering by tenant** (`schema_manager.py:68-71`): on collision it returns the *other tenant's* ACTIVE schema, touches it, and the materializer then DROPs/recreates `raw_*` tables inside it — cross-tenant overwrite, and both tenants subsequently query one shared schema.
- Separately, `_sanitize_schema_name` has no length bound. PostgreSQL silently truncates identifiers to 63 bytes, so two long external_ids sharing a 63-byte prefix collapse into one physical schema while their `TenantSchema.schema_name` values (255 chars) stay distinct. The team already fixed exactly this class for *view prefixes* after the 2026-06-10 incident (`_view_prefix`, `schema_manager.py:219-241`, with digest fallback and a hard length check in `build_view_schema`) — the sibling site, the schema name itself, was not swept. The review prompt's "fixed where it bit, not where it lives" pattern, verbatim.

### F8. Live-query artifacts bypass multi-tenant routing — BROKEN-NOW for multi-tenant workspaces / correctness

**Confidence: strong-inference (code path verified; depends on multi-tenant live artifacts existing in prod). Complexity: accidental.**

`ArtifactQueryDataView` resolves `tenant = await artifact.workspace.tenants.afirst()` then `load_tenant_context(tenant.external_id)` (`apps/artifacts/views.py:795-800`). In a multi-tenant workspace the agent wrote `source_queries` against the `ws_*` view schema's `prefix__table` names (that is the only surface it can query — `load_workspace_context` routes it there). At render time those queries execute with `search_path` set to the *first tenant's* schema: namespaced view names don't exist there, so every query errors ("relation does not exist") — or, if the agent ever wrote a raw `raw_*` query, it silently returns one tenant's slice presented as workspace-wide. Single-tenant workspaces work, which is why this survives demos. Every other consumer (chat, MCP, status) was migrated to `load_workspace_context`; this one wasn't.

### F9. Artifact subsystem remnants: dead provenance, phantom URL, dual data mechanisms with no migration story — DEBT / velocity+correctness

**Confidence: verified-by-trace for each item. Complexity: accidental.**

- `_build_tools` calls `create_artifact_tools(workspace, user)` with no `conversation_id` (`apps/agents/graph/base.py:694`), so every artifact stores `conversation_id=""` (`apps/agents/tools/artifact_tool.py` create path). The thread-attribution feature the model documents is dead. (`_get_thread_artifacts` in `apps/chat/thread_views.py:52` presumably matches on it — i.e., per-thread artifact lists can never populate from this path.)
- The tool returns `render_url = f"/artifacts/{id}/render/"` — no such route exists anywhere (`config/urls.py`, `apps/artifacts/urls.py`); the real sandbox is `/api/workspaces/<ws>/artifacts/<id>/sandbox/`. The agent can present users a dead link.
- Static `data` vs `source_queries` coexist exactly as the prompt suspected; stored SQL is free text with no link to the schema objects it names. When materialization renames tables (e.g., transformation terminal models replacing `raw_*`, or the F1/F5 teardown paths), artifacts break with no detection — there is no inventory of stored-SQL consumers to migrate. `ArtifactExportView` PNG/PDF returns 501 with a message pointing at "the async endpoint" that does not exist (`apps/artifacts/views.py:980-986`).

### F10. Table-catalog logic exists in four variants; prompt and tools can disagree — DEBT / correctness+velocity

**Confidence: verified-by-trace (call graph). Complexity: mostly accidental.**

Implementations: (a) `pipeline_list_tables` (run-record ∩ information_schema), used by MCP `list_tables`; (b) `transformation_aware_list_tables`, used **only** by the system-prompt builder (`apps/agents/graph/base.py:249`) — the MCP `list_tables` tool never applies transformation awareness, so when transformation assets exist the agent's prompt advertises terminal models while its `list_tables` tool returns the raw tables; (c) `workspace_list_tables` (information_schema only, no run reconciliation) for view schemas; (d) `DataDictionaryView._get_from_pipeline` (its own merge of the same parts, sync, plus `stg_` filtering), and (e) `get_schema_status` builds its `tables` from `run.result` without live reconciliation (`mcp_server/server.py:715-724`) — the one place phantom tables can still appear post-#185. Each variant was added for one consumer; none share a single catalog service.

### F11. The materialization/ThreadJob subsystem is a hand-rolled distributed transaction — DEBT (mixed essential/accidental) / velocity

**Confidence: verified-by-trace (structure). Complexity: mixed.**

`apps/workspaces/tasks.py` (1,289 lines) carries ~15 incident annotations (June-2026 dead-connection incident, #185, #187, #190, #198, the FutureApp janitor bug, the Stop-race fix…). Structural compensations stacked on the MCP→worker seam:
- `_defer_resume_for_job` sleep-retry hedge (0/0.25/0.5/1/2s) because MCP commits the ThreadJob *after* `defer_async` (tasks.py:363-395) — the TODO in the docstring names the real fix (nullable `procrastinate_job_id`, write-before-dispatch) and defers it.
- A janitor (15-min cron) plus an API-side poll backstop (`reconcile_stale_thread_job`) because the janitor lives in the worker that may itself be sick.
- CAS-scoped claims and terminal flips on ThreadJob with comment-documented reasons for each state subset (CLAIMABLE excludes RUNNING; CANCELLED gets one resume; etc.).

The background-resume requirement is essential complexity. What's accidental is that *three* row types (ThreadJob, procrastinate job, MaterializationRun) each hold part of the lifecycle with no single owner, so every fix must be re-proven against all pairwise races. The git history of this file is a textbook fix-chain (12+ consecutive `fix(workspaces): resume/janitor/CAS` commits).

### F12. Tenancy trust model is asymmetric between single- and multi-tenant workspaces — DEBT / security-consistency

**Confidence: verified-by-trace (code); intent unverified. Complexity: unclear (may be intended sharing semantics, undocumented).**

`_resolve_workspace_and_membership` (`apps/chat/helpers.py:88-120`): single-tenant chat requires a `TenantMembership` (provider-verified data access); multi-tenant chat requires only `WorkspaceMembership` — a shared user can query *all* tenants' data with zero provider-side standing. So combining two tenants into a workspace *lowers* the bar for accessing each. If workspace-membership-as-grant is the intended sharing model, the single-tenant TenantMembership check is vestigial friction; if provider-verified access is the model, multi-tenant is a hole. The codebase never states which. Materialization credentials still come from members who *do* have TenantMemberships, which masks the question day-to-day.

### F13. Account model: auto-merge is structurally unreachable for its main case; email-trust settings deserve a security pass — LATENT / security

**Confidence: verified-by-trace for the merge gate; hypothesis for provider email trust.**

- `reconcile_existing_user_on_login` (`apps/users/signals.py`) refuses to merge unless the canonical user has a **verified** `EmailAddress`. `ACCOUNT_EMAIL_VERIFICATION = "optional"` (`config/settings/base.py:199`) means password-signup users essentially never verify, so the auto-merge built for "password account, later OAuth login" cannot fire for precisely that population — duplicates persist until an operator runs `merge_duplicate_users` (the refusal is the secure choice; the incoherence is shipping an auto-merge whose precondition the product never produces).
- `SOCIALACCOUNT_EMAIL_AUTHENTICATION = True` + `AUTO_CONNECT = True` + `SOCIALACCOUNT_EMAIL_VERIFICATION = "none"` (base.py:207-214) makes provider-asserted emails an authentication factor for linking to existing accounts. For Google/GitHub that's fine; for the three custom providers it depends on whether CommCare/Connect/OCS verify the emails they return — unverified upstream email = account takeover vector. Not verifiable from this repo; flagged for the security lens.
- The OAuth domain allow-list deliberately admits logins that return no email (`apps/users/adapters.py:pre_social_login` docstring + code) — a documented bypass of the restriction feature.

### F14. Rename residue across three generations of naming — DEBT / velocity

**Confidence: verified-by-trace. Complexity: accidental.**

projects → workspaces → (frontend) "domains": `frontend/src/store/domainSlice.ts` and `activeDomainId` everywhere; `Workspace.data_dictionary` "legacy fields retained" with a live fallback path in `TableDetailView._get_table_data`; compat shims `Workspace.tenant`/`external_tenant_id`/`tenant_name` (consumed by the broken recipe runner — the shims keep dead code compiling); `create_refresh_schema` docstring referencing Celery; `QueryContext.tenant_id` holding a workspace UUID in the multi-tenant path (`mcp_server/context.py:134`). Each shim is small; together they make the next refactor (and AI-assisted edits) error-prone — F2 is what happens when a shim keeps old call-shapes plausible.

### F15. System-prompt builder does heavy per-turn metadata work with a 60s cache — DEBT / cost-perf

**Confidence: verified-by-trace (structure); cost impact strong-inference.**

`_build_system_prompt` → `_fetch_schema_context` runs `pipeline_describe_table` per table (one managed-DB connection each via `_execute_async_parameterized`) on every cache miss (`apps/agents/graph/base.py:259-272`), keyed per (workspace, user, prompt-hash) with a 60-second TTL and per-process cache — the worker, API, and any horizontal replicas each redo it. Embedding the full schema in the system prompt also changes the prompt text whenever a row count or timestamp changes, which defeats Anthropic prompt caching across materializations. Fine at current scale; worth knowing it's O(tables) DB connections per active user per minute.

---

## 4. Cross-cutting patterns

1. **Fixed-where-it-bit, not where-it-lives.** 63-byte identifier truncation: fixed for view prefixes, not for schema names (F7). Phantom-table reconciliation: fixed in `pipeline_list_tables`, not in `get_schema_status` (F10). Teardown bookkeeping: correct in the worker task, absent in the MCP tool (F5). TTL-touch-on-provision: fixed in `provision()`, while the refresh path that motivated schema lifecycle work remains broken outright (F1).
2. **Same problem, N implementations.** Cancellation ×3 (F6); catalog ×4-5 (F10); teardown ×2 (F5); workspace resolution ×4 (these at least share semantics); schema-status derivation in `_derive_schema_status` (workspace_views) vs `get_schema_status` (MCP) vs `RefreshStatusView`.
3. **Cross-cutting refactors stop at the chat path.** Workspaces refactor missed recipes (F2) and the refresh endpoint (F1); multi-tenant view-schema routing missed artifacts (F8); transformation-awareness missed the MCP catalog (F10). The shared root cause: consumers reach *around* the abstractions (raw `tenants.first()`, direct `load_tenant_context`, free-text SQL) instead of through one resolver, so the compiler/tests can't flag the stragglers.
4. **Mocks at exactly the seams that break.** `run_pipeline` mocked in refresh tests (hides F1); recipe tests presumably inject `graph` (the constructor's `graph=` parameter exists *for tests*, hiding F2); MCP chat integration tests are extensive but in-process. The test suite is large (52k LOC total Python, ~40% tests) yet the three BROKEN-NOW findings all live behind a mock.
5. **Role/permission enforcement is endpoint-local, not boundary-local.** DRF endpoints each re-implement role checks (or don't); the agent/MCP boundary — the most powerful surface — has none (F4). `user_role: "analyst"` is carried through state and never read.
6. **Incident archaeology as comments.** tasks.py/materializer.py comments are excellent *and* are doing the job a design doc should: the invariants (who may flip which state when) exist only as prose distributed across call sites.

---

## 5. Recommendations (prioritized)

**Now (days):**
1. **Disable or fix the refresh endpoint (F1).** Cheapest safe fix: make `RefreshSchemaView` dispatch `materialize_workspace` (the maintained path) and delete `refresh_tenant_schema` + `create_refresh_schema`; the blue/green design is already dead. One day, removes a data-destroying button.
2. **Wire role checks into the MCP boundary (F4):** `run_materialization` and `teardown_schema` should resolve WorkspaceMembership.role (the user_id is already injected); teardown should require MANAGE. ~1 day.
3. **Fix or feature-flag recipes (F2).** If recipes matter: pass `workspace=`, current state keys, and real `mcp_tools`; add one unmocked integration test that runs a trivial recipe. If not: hide the Run button rather than shipping a guaranteed 500. 1–2 days.
4. **Artifact sandbox origin (F3):** drop `allow-same-origin` (deliver artifact data via postMessage instead of the credentialed fetch — half the channel already exists) or move the sandbox to a dedicated origin. 2–4 days; do this before any external sharing/embed expansion (an `EmbedPage` and embed middleware already exist).

**Next (weeks):**
5. **Single catalog + single routing service.** One `resolve_query_surface(workspace) -> QueryContext` and one `list_catalog(workspace)` that every consumer (MCP tools, prompt builder, data dictionary, artifacts, status) must use; fold transformation-awareness and information_schema reconciliation in once. Unblocks F8/F10 and prevents the next F8.
6. **Unify teardown/cancel into the task layer (F5, F6):** MCP tools should *defer the existing worker tasks* (or call one shared service), never reimplement DDL + state flips. Add the schema-name length/provider-namespace guard while in there (F7) — new tenants get `{provider_short}_{sanitized}_{digest}` names; existing schemas can be grandfathered via the TenantSchema row.
7. **Stored-SQL inventory & migration discipline:** a registry of stored references to schema objects (artifact `source_queries`, knowledge `TableKnowledge.table_name` — which embeds `schema.table` and goes stale on every schema change — golden queries, recipes) and a check that runs after materialization-shape changes. This is the prompt's "materialization renamed tables and broke artifacts" asked for as a system.

**Guardrails (cheap, ongoing):**
8. Ban `workspace.tenants.first()`/`.tenant` outside the routing service (lint rule on the compat shims; then delete the shims).
9. One seam test per process boundary with real objects: refresh→pipeline (would have caught F1), recipe→graph (F2), artifact→query routing (F8). Rule of thumb: any `patch(...run_pipeline...)`-style mock at a process seam needs a sibling unmocked test.
10. Promote the tasks.py comment-archaeology into a short state-machine doc (who may write ThreadJob/MaterializationRun states, from which process), and require new states/writers to update it.

---

## 6. What's actually fine

- **SQL safety layer**: sqlglot-based validator (single statement, SELECT-only, 40+ function blocklist, limit injection) *plus* `SET ROLE <schema>_ro` at execution (`mcp_server/services/query.py:44`) — proper defense in depth, not just regex theater.
- **Materializer state transitions**: every phase flip is a conditional UPDATE preserving externally-set CANCELLED; per-source isolation, per-page resumable cursors with honest `in_progress` semantics, terminal-state guarantees on all failure paths. The design notes at the top of `materializer.py` match the code (I checked).
- **User merge service** (`apps/users/services/merge.py`): explicit conflict resolution per relation, `_meta`-driven long-tail repointing, dry-run, single transaction. Better than most hand-written merges.
- **View-schema build** (`build_view_schema`): pre-flight collision and 63-byte checks on final names, drop-and-recreate idempotency, error text persisted to `last_error` and surfaced through agent/MCP/API.
- **Chat endpoint hygiene**: thread-ownership check with anti-enumeration 404, message length cap, CSRF, rate limiting, dangling-tool-call repair on both write and read paths.
- **Workspace resolution helpers**: 4 variants but identical semantics (membership-scoped lookup, uniform 403), consistently used across DRF/async views.
- **Token handling**: Fernet encryption of OAuth tokens at rest via the allauth adapter; credentials encrypted; fail-closed OCS team guard in credential resolution.
- **Async conventions**: the CLAUDE.md rules (async ORM, no sync-from-async) are actually followed; the thread-local connection hygiene in `_run_pipeline_with_progress` shows the failure modes are understood.

---

## 7. Coverage appendix (honest)

**Deep-read (line-by-line):** `apps/workspaces/tasks.py`, `apps/workspaces/models.py`, `apps/workspaces/services/schema_manager.py`, `apps/workspaces/services/workspace_service.py`, `apps/workspaces/api/views.py`, `apps/workspaces/api/materialization_views.py` (cancel view), `apps/workspaces/workspace_resolver.py`, `apps/workspaces/permissions.py`, `mcp_server/server.py`, `mcp_server/context.py`, `mcp_server/services/metadata.py`, `mcp_server/services/materializer.py` (lines 1–1510 of 1973; writers beyond skimmed), `apps/agents/graph/base.py`, `apps/agents/tools/artifact_tool.py`, `apps/chat/views.py`, `apps/chat/helpers.py`, `apps/artifacts/views.py`, `apps/artifacts/models.py`, `apps/artifacts/urls.py`, `apps/users/models.py`, `apps/users/adapters.py`, `apps/users/signals.py`, `apps/users/services/merge.py`, `apps/users/services/tenant_resolution.py`, `apps/recipes/services/runner.py` (partially truncated mid-file; execute paths read), `apps/recipes/api/views.py` (run views).

**Skimmed (structure/greps only):** `mcp_server/services/sql_validator.py`, `mcp_server/services/query.py`, `mcp_server/loaders/connect_base.py`, `apps/chat/thread_views.py`, `apps/chat/stream.py`, `apps/workspaces/api/jobs_views.py`, `apps/workspaces/api/workspace_views.py`, `apps/transformations/services/executor.py` (function list only), `config/settings/base.py` (auth section only), frontend: `dictionarySlice.ts`, `recipeSlice.ts`, `ArtifactPanel.tsx`, `DataDictionaryPage.tsx`, `slashCommands.ts` (grep-level), git history (log + per-file churn).

**Not examined at all:** `apps/knowledge/` (retriever, models, eval/golden queries), `apps/users/views.py` and the three custom OAuth provider packages (`providers/commcare*`, `providers/ocs`), `apps/users/services/credential_resolver.py` / `token_refresh.py` / `api_key_providers/`, `apps/chat/checkpointer.py` / `message_converter.py` / `rate_limiting.py` internals, `apps/transformations/` models/lineage/commcare_staging (dbt subsystem essentially uncovered), all loaders except `connect_base` head, `mcp_server/auth.py` and `envelope.py`, `mcp_server/pipeline_registry.py`, materializer lines 1510–1973, `apps/artifacts/services/export.py`, `apps/recipes/models.py`, embed middleware + `EmbedPage` (embed/widget security untouched), deployment configs (`config/deploy*.yml`, Dockerfile, kamal, CI), `config/procrastinate.py` (the `task` decorator's connection hygiene — referenced but unread), the entire frontend beyond the files above (router, contexts, hooks, ChatPanel internals), all migrations, the test suite contents (only grepped for mocking patterns), management commands, admin.py files, docs/plans (titles only).

Per the methodology: under-claiming is intended — knowledge/transformations/providers/embed/frontend are genuine cold zones for this reviewer.
