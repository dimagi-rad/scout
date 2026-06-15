# Journey tracer: multi-tenant & lifecycle journeys

*Reviewer: journey-multi-tenant (Phase 1, arch review v2). Report-only; no code changed.*

Journeys traced end-to-end:

- **J1**: Two tenants (different providers) join one workspace → custom transformation applied → materialization re-runs → every downstream consumer re-checked (view schema, MCP catalog, prompt context, artifacts, dictionary, recipes, knowledge).
- **J2**: Workspace idle past TTL (24h, `SCHEMA_TTL_HOURS`, `config/settings/base.py:345`) → janitor expires schemas → user returns and exercises every feature.
- **J3**: Single-tenant workspace → user clicks the Data Dictionary "refresh" → background refresh task.
- **J4**: Multi-user multi-tenant workspace — user A owns tenant 1's credential, user B owns tenant 2's; A asks a question that triggers materialization.

Severity scale per the shared evidence standards: Status `BROKEN-NOW/LATENT/DEBT/COSMETIC`, impact `data-loss/security/correctness/cost-perf/velocity`, confidence `verified-by-trace/strong-inference/hypothesis`.

---

## F1 — Recipe runs crash with TypeError: runner still calls the pre-rename `build_agent_graph` signature  *(BROKEN-NOW · correctness · verified-by-trace)*

The agent-graph builder takes `workspace` as its first required parameter:

- `apps/agents/graph/base.py:480-486` — `async def build_agent_graph(workspace: Workspace, user=None, checkpointer=None, mcp_tools=None, oauth_tokens=None)`

The recipe runner calls it with a keyword that no longer exists and without `workspace`:

- `apps/recipes/services/runner.py:115-119` —
  `build_agent_graph(tenant_membership=self._tenant_membership, user=self.user, checkpointer=None)`

**Chain**: `POST /api/workspaces/<ws>/recipes/<id>/run/` → `RecipeRunView.post` (`apps/recipes/api/views.py:107-108`) → `RecipeRunner.execute` (`runner.py:185-191`, `graph = async_to_sync(self._build_graph)()`) → `_build_graph` (`runner.py:115`) → **TypeError: build_agent_graph() got an unexpected keyword argument 'tenant_membership'** → caught at `views.py:109-111` → HTTP 500 `{"error": "..."}` to the user.

Three compounding defects behind the same seam:

1. The `RecipeRun` row is created with `status=RUNNING` (`runner.py:123-134`) *before* the graph build at line 191, which is **outside** the `try` starting at line 213 — every failed run strands a `RUNNING` RecipeRun row forever (no janitor covers RecipeRun).
2. Even with the signature fixed, `initial_state` (`runner.py:215-224`) sets `tenant_id` / `tenant_name` / `tenant_membership_id`, none of which exist in `AgentState` (`apps/agents/graph/state.py:80-148`: `workspace_id`, `user_id`, `user_role`, `thread_id`). MCP tool injection (`graph/base.py:504-508`) would inject `workspace_id=""` and every data tool call would fail validation (`mcp_server/context.py:73-74` via `server.py:71-75`).
3. `mcp_tools` is never passed, so the recipe agent would have **no data tools at all** (only save_learning/artifact/recipe tools, `graph/base.py:692-696`).

**Reachability**: live UI — RecipesPage run action → `frontend/src/store/recipeSlice.ts:135` posts to the run endpoint.

**History**: `build_agent_graph` has been workspace-first since the multi-tenant refactor (`e26cd75`, 2026-03-10); the runner's `tenant_membership=` call predates that and has only been touched by lint refactors since (`27897fc`, `08b4673`). The recipes feature has most likely been broken at runtime for ~3 months; tests presumably pass a `graph=` override (the constructor accepts one, `runner.py:65-75`), hiding the seam.

Complexity: accidental (rename residue at a seam with no integration test).

---

## F2 — Artifact live queries in multi-tenant workspaces execute against the wrong schema  *(BROKEN-NOW · correctness · verified-by-trace)*

In a multi-tenant workspace the agent can only see and query the namespaced views of the view schema (`ws_<hash>`, names like `{prefix}__{table}`):

- prompt context: `apps/agents/graph/base.py:299-393` (`_MULTI_TENANT_NAMESPACE_HINT`, `workspace_list_tables`)
- MCP query routing: `mcp_server/context.py:83-139` (multi-tenant → view schema)

So artifacts created in such a workspace carry `source_queries` referencing `prefix__table` views. But the artifact data endpoint resolves the **first tenant's tenant schema** instead of the workspace context:

- `apps/artifacts/views.py:795` — `tenant = await artifact.workspace.tenants.afirst()`
- `apps/artifacts/views.py:800` — `ctx = await load_tenant_context(tenant.external_id)`

**Chain**: open artifact in UI → `GET .../artifacts/<id>/query-data/` → `ArtifactQueryDataView.get` → `load_tenant_context(first_tenant)` → `execute_query` with `search_path = t_<first_tenant>` (`mcp_server/services/query.py:44-50`) → `relation "{prefix}__{table}" does not exist` → every query in the artifact renders an error card (`views.py:824-831`).

Every live artifact created in a multi-tenant workspace is dead on arrival. Conversely, artifacts created *before* a workspace became multi-tenant keep "working" by accident — they query the first tenant's raw tables directly, silently ignoring the second tenant's data.

Complexity: accidental — `load_workspace_context` exists and is exactly the right resolver; this view predates multi-tenancy and was never migrated (same class of drift the v1 reviews called artifacts↔tenancy).

---

## F3 — Data-dictionary "Refresh" destroys the freshly-loaded data and activates an empty schema  *(BROKEN-NOW · data-loss · verified-by-trace; confirms v1 S1 and adds detail)*

**Chain**:

1. UI: DataDictionaryPage refresh button → `frontend/src/store/dictionarySlice.ts:197` → `POST /api/workspaces/<id>/refresh/`.
2. `RefreshSchemaView.post` (`apps/workspaces/api/views.py:352-365`) → `create_refresh_schema` (`schema_manager.py:169-181`) creates a **new** TenantSchema record named `{sanitized}_r{8hex}` (PROVISIONING), then defers `refresh_tenant_schema`.
3. `refresh_tenant_schema` (`apps/workspaces/tasks.py:126-200`): creates the new physical schema (line 150), then calls `run_pipeline(membership, credential, pipeline_config)` (line 173).
4. `run_pipeline` (`mcp_server/services/materializer.py:183`) does **`SchemaManager().provision(tenant_membership.tenant)`** — provision resolves by the *sanitized external_id* name (`schema_manager.py:66-78`) and returns the **old ACTIVE schema**. All data is loaded there, not into the `_r` schema.
5. Back in the task: the new, **empty** `_r` schema is marked ACTIVE (`tasks.py:182-184`); every other ACTIVE schema for the tenant — including the one the data was just loaded into — is flipped to TEARDOWN and dropped 30 minutes later (`tasks.py:188-197` → `teardown_schema`, which also flips its COMPLETED/PARTIAL runs to STALE, `tasks.py:639-645`).

**Net effect**: the refresh consumes a full provider sync, then destroys its own output; the workspace is left with an empty ACTIVE schema and a STALE catalog. In a multi-tenant workspace the dropped schema additionally cascade-drops the namespaced views and flips the WorkspaceViewSchema to FAILED (`tasks.py:647-653`) — and `refresh_tenant_schema` never triggers a sibling/own view-schema rebuild, unlike `materialize_workspace`.

Secondary defect even if the schema targeting were fixed: refresh deliberately rotates schema *names* (`demo` → `demo_r1a2b3c4`), but `TableKnowledge` annotations are keyed by qualified name `f"{schema_name}.{table_name}"` (`apps/workspaces/api/views.py:289-290`), so every dictionary annotation would orphan on each refresh.

Reachable: yes, button on a primary page. Complexity: accidental — two provisioning models (provision-by-tenant-name vs. explicit-schema-record) coexist and the pipeline only knows the first.

---

## F4 — Tenant identity collapses to the sanitized `external_id`; cross-provider/cross-tenant collision shares one physical schema  *(LATENT · data-loss + security · mechanism verified-by-trace)*

`Tenant` uniqueness is `(provider, external_id)` (`apps/users/models.py:123-124`) — the same `external_id` may exist under two providers, and two distinct ids can sanitize to the same string (`schema_manager.py:625-631` strips `-` → `_` and non-alnum: `my-org` and `my_org` collide even within one provider).

Three places key on the collapsed identity:

1. **`SchemaManager.provision`** (`schema_manager.py:66-78`) looks up `TenantSchema` by `schema_name` **only** and returns whatever it finds — including a schema row whose FK points at a *different* tenant. The materializer (`materializer.py:183`) then drops and recreates that schema's `raw_*` tables with the second tenant's data (writers all `DROP TABLE IF EXISTS ... CASCADE`, e.g. `materializer.py:851,900,961`). Tenant A's workspace now serves tenant B's data: simultaneous data destruction and cross-tenant disclosure.
2. **`load_tenant_context`** (`mcp_server/context.py:56-59`) resolves `tenant__external_id=tenant_id` with no provider qualifier and `.afirst()` — ambiguous routing for same-`external_id` tenants across providers.
3. **`build_view_schema`** (`schema_manager.py:306`) does `Tenant.objects.get(external_id=...)` — raises `MultipleObjectsReturned` and fails the whole multi-tenant build if two providers share an external_id anywhere in the database (not even in the same workspace).

Status LATENT because it needs a name collision to fire, but the input-validation family (seed #12: connect-name truncation, 63-byte truncation) shows identifier-shape collisions are this codebase's recurring real-world bug class. CommCare domain names allow hyphens; nothing prevents the colliding pair.

Complexity: accidental — the schema name is being used as the identity key instead of the `Tenant` PK that already exists on the row.

---

## F5 — Data Dictionary (and refresh/status APIs) are single-tenant-only: multi-tenant workspaces see one tenant's tables under the wrong names  *(BROKEN-NOW for multi-tenant workspaces · correctness · verified-by-trace)*

`DataDictionaryView.get` (`apps/workspaces/api/views.py:236-250`) and `TableDetailView` (`views.py:479-506`) resolve `workspace.tenant` — the **first** tenant by `canonical_name` ordering (`apps/workspaces/models.py:143-146`). For a multi-tenant workspace the dictionary therefore:

- shows only the first tenant's tables (second tenant invisible),
- shows them under raw names (`raw_visits`) while the agent and every query surface use `prefix__raw_visits` view names — annotations written here can never match what the agent queries,
- `RefreshSchemaView` / `RefreshStatusView` (`views.py:336`, `views.py:387`) likewise act on the first tenant only.

Reachable: Data Dictionary page for any multi-tenant workspace. Complexity: accidental (view predates multi-tenancy; the "Legacy fields retained" comment at `models.py:131` documents the unfinished migration).

---

## F6 — Workspace-scoped custom transformations never re-run on materialization; they silently serve stale data  *(LATENT · correctness · verified-by-trace for the code path, strong-inference for user impact)*

- The materializer's transform phase calls `run_transformation_pipeline(tenant=..., schema_name=...)` with **no `workspace`** (`mcp_server/services/materializer.py:1060-1065`).
- The executor only appends the workspace stage when `workspace` is passed (`apps/transformations/services/executor.py:62-69`).
- dbt assets are materialized as **tables** (`apps/transformations/services/dbt_project.py` — `{"+materialized": "table"}`), so a workspace-scoped model's table *survives* the raw-table `DROP ... CASCADE` during reload.

**Journey J1 consequence**: user applies a workspace-scoped transformation (created via `/api/transformations/assets/`, `TransformationAssetViewSet.perform_create`), materialization re-runs (agent `run_materialization` → `materialize_workspace` → `run_pipeline` → transform phase) — system and tenant stages re-run, the **workspace stage does not**. The custom table keeps its pre-refresh contents; in a multi-tenant workspace `build_view_schema` re-publishes a view over it (it views every BASE TABLE, `schema_manager.py:328-344`), so the stale table is presented next to fresh data with no marker. The only way to refresh it is the synchronous manual trigger `POST /api/transformations/runs/trigger/` with `workspace_id` (`apps/transformations/views.py:121-166`) — which also runs dbt inline in the request thread (cost/timeout exposure).

Tenant-scoped custom assets are fine (tenant stage runs every materialization).

---

## F7 — Transformation catalog advertises terminal assets without checking they physically exist  *(LATENT · correctness · strong-inference)*

`transformation_aware_list_tables` appends every terminal `TransformationAsset` to the table list unconditionally (`mcp_server/services/metadata.py:312-323`) — no reconciliation against `information_schema`, unlike the raw-table path right above it (`metadata.py:76-84`, the #185 phantom-rows fix). Transform failures are isolated and swallowed (`executor.py:85-92`; `materializer.py` transform phase failures "do not fail the overall data load"), so a failed dbt run leaves the asset row present and the physical table absent/stale. The prompt context uses the same function (`graph/base.py:246-251`), so the agent is *told* the table exists, queries it, gets `NOT_FOUND`, and after three errors trips the escalation circuit breaker (`graph/base.py:87-123`) — the exact #190 panic-loop family, reintroduced one layer up.

---

## F8 — Multi-tenant catalog bypasses every truth filter the single-tenant catalog has  *(LATENT · correctness · verified-by-trace, static)*

Single-tenant listing (`pipeline_list_tables`, `metadata.py:29-112`) excludes: sources not `completed` (including `in_progress` mid-resume partial loads, issue #187), tables that no longer physically exist, and (at the dictionary level) `stg_*` staging tables.

Multi-tenant listing (`workspace_list_tables`, `metadata.py:161-185`) returns **every view** in the view schema — and `build_view_schema` creates a view for **every table and view** in each tenant schema (`schema_manager.py:328-344`), including `stg_*` models and partially-loaded `raw_*` tables from an interrupted resumable Connect load.

Consequence on J1: while tenant A's `completed_works` is mid-resume (`in_progress`, cursor saved), the single-tenant surfaces hide it, but a multi-tenant workspace shows `tenantA__raw_completed_works` in `list_tables` and the system prompt with no warning, contradicting the resume-prompt instruction ("do NOT query its table as if it were complete", `tasks.py:1109-1118`). This is the sixth table-catalog implementation, and it disagrees with the other five (seed #6).

---

## F9 — `SchemaState.MATERIALIZING` is never written; single-tenant in-flight detection is dead, and same-tenant concurrent materializations are possible  *(LATENT · correctness/data-integrity · verified-by-trace for the dead state; race acknowledged in-code)*

No code path ever sets `state=MATERIALIZING` (grep over `apps/` + `mcp_server/`: only filters and equality reads; writers set PROVISIONING/ACTIVE/TEARDOWN/EXPIRED/FAILED). Therefore:

- `_fetch_schema_context`'s "materialization already in progress, do NOT trigger another" branch (`graph/base.py:230-237`) is **unreachable** for single-tenant workspaces. (The multi-tenant branch works because it checks `MaterializationRun.ACTIVE_STATES` instead, `graph/base.py:318-332` — two different mechanisms for the same question.)
- During a single-tenant materialization the schema sits ACTIVE (provision marks ACTIVE immediately, `schema_manager.py:114-122`), so a second chat thread's prompt says "Data is loaded but no tables are available yet" or "No data loaded yet → run_materialization".
- `run_materialization`'s dedupe guard is **thread-scoped only** (`mcp_server/server.py:572-604`), and the comment explicitly concedes: parallel materializations of the same tenant schema from two threads/workspaces are possible and "the materializer has no advisory lock per tenant_schema". Two concurrent pipelines `DROP TABLE`/`CREATE TABLE`/insert into the same physical tables.

Also makes `_schema_status_for_workspaces`' PROVISIONING/MATERIALIZING handling (`workspace_views.py:85`) and several `state__in=[ACTIVE, MATERIALIZING]` filters half-dead. Complexity: accidental (vestigial state from an earlier design).

---

## F10 — TTL liveness depends on the chat endpoint; every non-chat surface under-touches  *(DEBT · correctness/cost-perf · verified-by-trace)*

`touch_workspace_schemas` — the only call that touches *all* constituent schemas of a multi-tenant workspace — is called exactly once in the codebase, from the chat view (`apps/chat/views.py:151`). Everything else touches partially or not at all:

| Surface | What it touches | Gap |
|---|---|---|
| MCP query/list (multi-tenant) | view schema only (`context.py:125`) | constituent tenant schemas age — mitigated only because chat precedes agent activity |
| Artifact query-data | resolved (first-)tenant schema (`artifacts/views.py:810-812`) | view schema + other tenants age |
| Data dictionary browsing | nothing | pure read; 24h of dictionary-only use → schemas expire under the user |
| Recipe run endpoint | first tenant only (`recipes/api/views.py:116-122`) | moot while F1 holds |
| Resume task / janitors | nothing (by design) | — |

When a constituent tenant schema expires, `teardown_schema` cascade-drops the namespaced views and flips the still-in-use WorkspaceViewSchema to FAILED (`tasks.py:647-653`) — correct bookkeeping, but the workspace loses its query surface mid-use for users whose activity pattern didn't route through chat.

**Recovery-cost corollary (J2)**: when only the *view schema* has expired (tenant data intact), `load_workspace_context` errors and the prompt context tells the agent "No data has been loaded yet → run_materialization" (`graph/base.py:334-343`). The agent's only recovery tool re-syncs **every tenant from the provider APIs** (minutes–hours, API quota) to recreate what `rebuild_workspace_view_schema` (`tasks.py:556-584`) would rebuild in milliseconds — but no agent tool and no API endpoint exposes the cheap rebuild (the `context.py:122` error text suggests `POST /api/workspaces/<id>/tenants/`, which only rebuilds as a side effect of re-adding a tenant).

---

## F11 — Multi-user multi-tenant workspace: materialization is requester-scoped, and the failure message then gives the wrong instruction  *(LATENT · correctness · verified-by-trace for the message path)*

`run_materialization` passes the calling user's id (`server.py:607-610`); `materialize_workspace` filters memberships to that user (`tasks.py:240-241`). In journey J4 (A owns tenant 1's connection, B owns tenant 2's):

1. A triggers materialization → only tenant 1 loads → `all_succeeded=True` for A's subset.
2. `build_view_schema` requires **every** workspace tenant to have an ACTIVE schema (`schema_manager.py:269-275`) → raises "Tenant '<t2>' has no active schema" → vs FAILED.
3. The resume task detects the failed view schema and instructs the agent: *"Do NOT re-run materialization — it cannot fix this ... a system-side fix is required"* (`tasks.py:1085-1095`), and `ThreadJob` terminal state is FAILED with the same error summary (`tasks.py:1243-1248`).

But here no system-side fix is needed — user B running materialization fixes it (B's run loads tenant 2, then `build_view_schema` finds both ACTIVE and succeeds). The hard "do NOT re-run" framing is wrong for the most ordinary multi-user setup the feature supports. Note also `_resolve_workspace_memberships` (`server.py:509`) doesn't filter `archived_at`, while the task does (`tasks.py:232`) — an archived-only user passes the authz guard and then materializes nothing.

---

## F12 — View-schema readonly role keeps SELECT/USAGE grants on removed tenants' schemas  *(DEBT · security (defense-in-depth) · verified-by-trace)*

`build_view_schema` grants the `ws_<hash>_ro` role USAGE + SELECT on every constituent tenant schema (`schema_manager.py:391-405`). On tenant removal, the rebuild path (`workspace_service.py:67-71` → `build_view_schema`) re-grants for current tenants but never **revokes** the removed tenant's grants; revocation only happens at role drop (`_drop_readonly_role`). The DB-level capability to read the removed tenant's tables therefore persists indefinitely.

Currently unreachable through the query tool: the SQL validator rejects schema-qualified references outside `{public, ctx.schema}` (`sql_validator.py:277-288`, `allowed_schemas=[]` at `query.py:31-35`). So this is a latent second-layer hole that becomes a live cross-tenant read the day anyone widens `allowed_schemas` or adds a raw-SQL surface using the same role.

---

## F13 — Single→multi tenant transition silently invalidates every stored reference  *(DEBT · correctness · verified-by-trace for the shapes; essential complexity, but unhandled)*

Adding a second tenant (`add_workspace_tenant`) flips the entire query surface from `t_<id>` raw names to `ws_<hash>` `prefix__table` view names. Nothing migrates or even flags the stored references created before the flip:

- `Artifact.source_queries` (raw names) — keep "working" against the first tenant only via the F2 bug;
- `TableKnowledge.table_name` keyed `schema.table` (`api/views.py:289`) — never matches again;
- `Learning.applies_to_tables` / SQL, `Workspace.data_dictionary` JSON — same;
- checkpointed conversations replay SQL with raw table names into a workspace where those names no longer resolve.

The reverse transition (removal back to single-tenant, `workspace_service.py:60-66`) breaks the prefixed-name artifacts instead. Per-direction this is essential complexity of renaming a query namespace, but there is no detection, no warning, and no inventory of affected objects (cartography seam §4 "stored free-text references").

---

## Verified healthy (things this journey exercised that held up)

- **Provision-resurrect TTL fix (#228)**: `provision()` sets `last_accessed_at=now` on both fresh-create and EXPIRED-resurrect paths (`schema_manager.py:114-122`), with the incident rationale documented; the EXPIRED→IntegrityError→fall-through path correctly reuses the row and recreates the physical schema.
- **Teardown ordering**: runs flipped STALE only *after* the physical DROP succeeds; failed DROP reverts the record to ACTIVE rather than stranding data invisible (`tasks.py:609-663` and the matching janitor comment at 516-534).
- **Cross-workspace cascade bookkeeping (post-#230)**: `_fail_dependent_view_schemas` (`tasks.py:666-687`) and `_rebuild_sibling_view_schemas` (`tasks.py:441-463`) correctly scope to multi-tenant siblings via the count-subquery (which itself documents and avoids the Django filter+aggregate trap).
- **View-name truncation guards (post-#227)**: bounded prefixes with stable digest, full-name collision detection, and a hard 63-byte check before any DDL (`schema_manager.py:219-350`).
- **View-schema failure surfacing (post-#229)**: `last_error` persisted on build failure (`schema_manager.py:425-430`), surfaced through `get_schema_status` (`server.py:743-764`), the resume prompt, and the status API's distinct "failed" state (`workspace_views.py:33-61`).
- **Resume-task state machine**: claim CAS excluding RUNNING, cancel-during-ainvoke non-clobber re-read, synthetic failure messages through the checkpointer (`tasks.py:1019-1289`).
- **Chat thread ownership and workspace binding** checks before streaming (`chat/views.py:109-137`).
- **Tenant add/remove endpoints**: manage-role gated, requester tenant-membership validated, last-tenant guard under `select_for_update` (`workspace_views.py:548-617`, `workspace_service.py:48-71`).
- **SQL validator schema fencing**: schema-qualified access restricted to the routed schema; SET ROLE + search_path per query (`sql_validator.py:269-288`, `query.py:38-65`).

## Cross-cutting observations

1. **The multi-tenant feature is half-migrated.** The chat/MCP/worker spine handles multi-tenancy carefully (routing, view schema lifecycle, sibling rebuilds, failure surfacing — visibly hardened by the June incidents). Every surface *off* the spine — artifacts (F2), dictionary (F5), refresh (F3), recipes (F1), transformations (F6) — still assumes `workspace.tenant` (the first tenant). The `Workspace.tenant` compat property (`models.py:143-146`) is the load-bearing enabler of this drift; each of its 8+ production call sites is a single-tenant assumption.
2. **Same question, N mechanisms.** "Is materialization in flight?" is answered by `TenantSchema.MATERIALIZING` (dead, F9), `MaterializationRun.ACTIVE_STATES` (multi-tenant prompt), ThreadJob ACTIVE_STATES (MCP dedupe), and `TenantSchema.PROVISIONING` (refresh guard). "What tables exist?" has six implementations, with the newest (`workspace_list_tables`) lacking every truth-filter the older ones earned through incidents (F7, F8).
3. **The TTL design wants a single choke-point.** Touch logic is correct where it exists but is sprinkled per-endpoint (chat: full; artifacts: partial; dictionary: none). A touch inside `load_workspace_context`/`load_tenant_context` covering constituent schemas — or simply making the janitor consult view-schema liveness before expiring constituents — would close the family.

## Coverage log

**Deep-read** (line-by-line): `apps/workspaces/tasks.py`, `apps/workspaces/models.py`, `apps/workspaces/services/schema_manager.py`, `apps/workspaces/services/workspace_service.py`, `mcp_server/context.py`, `mcp_server/services/metadata.py`, `mcp_server/services/query.py`, `mcp_server/services/sql_validator.py`, `apps/agents/graph/base.py`, `apps/agents/graph/state.py`, `apps/recipes/services/runner.py` (lines 1–230), `apps/recipes/api/views.py` (run endpoints), `apps/transformations/models.py`, `apps/transformations/views.py`, `apps/transformations/services/executor.py`, `apps/transformations/services/dbt_project.py`, `apps/workspaces/api/views.py` (lines 1–510), `apps/workspaces/api/workspace_views.py` (lines 1–140, 523–618), `apps/artifacts/views.py` (lines 740–900), `apps/chat/views.py` (lines 100–230), `mcp_server/server.py` (lines 60–810: list/describe/get_metadata/get_lineage/query/run_materialization/get_schema_status), `mcp_server/services/materializer.py` (lines 90–310, 840–965, 1060–1120).

**Skimmed**: `apps/users/models.py` (Tenant/TenantConnection sections), `apps/agents/memory/checkpointer.py` (docstring region), `mcp_server/services/materializer.py` (writer family — sampled 3 of ~14 writers), frontend `store/dictionarySlice.ts`, `store/recipeSlice.ts`, `pages/DataDictionaryPage` (grep-level), git history for the F1 signature drift.

**Not examined** (in-scope for my journeys but unopened — gap-loop candidates):
- Frontend at large: `WorkspaceSwitcher`, `useWorkspaceThreadSync` (the threadId-across-workspaces fix `00c423d` not re-verified), `ChatPanel`, `ArtifactPanel`, `WorkspaceDetailPage` materialization controls, public pages.
- `mcp_server/loaders/` (all 19 files) and the remaining materializer writers/cursor logic — the Connect-duplication sibling audit belongs to the loaders vertical.
- `apps/users/` auth/OAuth/merge/tenant_resolution/signals (accounts side of the accounts↔tenancy chain), `credential_resolver.py` internals.
- `apps/chat/thread_views.py`, `stream.py`, checkpointer internals, rate limiting.
- `apps/artifacts/services/export.py`, sandbox view, `widget.js` embed path.
- `apps/knowledge/services/retriever.py` internals, knowledge import/export.
- `apps/workspaces/api/jobs_views.py`, `jobs_cancel.py`, `materialization_views.py` (cancel/retry detail), management commands.
- `apps/transformations/services/lineage.py` (visibility scoping), `commcare_staging.py`, `dbt_runner.py`.
- `mcp_server/envelope.py`, `auth.py`, `pipeline_registry.py`, pipeline YAMLs.
- Tests (`tests/`, `tests/qa/`) — I inferred but did not verify that recipe tests inject `graph=`; the test-architecture lens should confirm what the mocks hide for F1/F3.
- Deploy configs, settings beyond `SCHEMA_TTL_HOURS`.
