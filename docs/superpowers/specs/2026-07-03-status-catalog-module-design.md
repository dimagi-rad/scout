# One status/catalog module — single source of world-state truth

**Date:** 2026-07-03
**Issue:** arch #251 — "One status/catalog module (single source of world-state truth)" (cluster `status-catalog-module`, wave 2, `design-gated`)
**Status:** Design — awaiting Brian's sign-off. **No implementation until approved.**
**Author:** design window (research: 2 read-only mapping passes over the status + catalog surfaces)

## Problem

"Has this workspace's data loaded, and what tables exist?" is answered by **~7 independent status derivations** and **5 divergent table-catalog listers**, computed from three different substrates (`MaterializationRun.result`, live `information_schema`, `TransformationAsset` ORM) with different state predicates. They disagree in ways the agent and the user can both see — this is the contradictory-schema-response class that produced the #190 panic loop (18 SQL queries across 25 turns because `list_tables` said a table existed and `describe_table`/`query` said `NOT_FOUND`).

Concrete, user-visible divergences (all verified against `main`):

1. **`last_synced_at` COMPLETED-only vs "loaded and ready" COMPLETED|PARTIAL.** The status API derives `last_synced_at` from `MaterializationRun.state=COMPLETED` only (`apps/workspaces/api/workspace_views.py:163-170, 310-319`), so a tenant whose runs always end `PARTIAL` shows `last_synced_at: null` in the UI, while the multi-tenant prompt says "Data is loaded and ready. Last updated: …" from a `COMPLETED|PARTIAL` filter (`apps/agents/graph/base.py:442-470`). Same data, opposite story.

2. **`SchemaState.MATERIALIZING` has 0 production writers and 15 readers.** No code in `apps/` or `mcp_server/` ever assigns `MATERIALIZING` (grep-verified; `provision()` goes `PROVISIONING→ACTIVE` at `schema_manager.py:106,140`; the materializer only touches `MaterializationRun.RunState`). Yet 15 sites branch on it (census in Appendix A). The single-tenant "materialization already in progress — do NOT trigger another" prompt branch (`base.py:318`) can therefore **never fire**; in-progress is actually carried by `MaterializationRun.state ∈ ACTIVE_STATES`, which is why the multi-tenant twin (`base.py:404-411`) redundantly also checks `active_run`.

3. **The "usable schema" predicate forks three ways.** During any multi-tenant rebuild window: the query router `load_workspace_context` accepts `WorkspaceViewSchema.state == ACTIVE` only (`mcp_server/context.py:121-125`); `get_schema_status` accepts `ACTIVE|MATERIALIZING` (a dead arm) (`server.py:824`); the prompt's "not ready" predicate is `vs is None OR vs.state != ACTIVE` (`base.py:422`). So during a rebuild `get_schema_status` can say `not_provisioned` (inviting a redundant `run_materialization`) while the query tool says "trigger a rebuild" though one is already running.

4. **`get_schema_status` reads an extinct result shape.** Single-tenant, it reads `last_run.result["tables"]` / `["table"]+["rows_loaded"]` (`server.py:802-808`), but the materializer persists only `{sources, pipeline, transforms}` (`materializer.py:473-477`). Neither key matches, so it always returns `tables: []` with `state: active` even after a fully successful run — disagreeing with `list_tables` in the same conversation (a #190 input).

5. **Five catalog listers disagree** on source-of-truth, physical-existence reconciliation, and `stg_*` filtering (matrix in Appendix B). The prompt's `transformation_aware_list_tables` advertises terminal `TransformationAsset`s **by name with no physical-existence check** (`metadata.py:309-320`), while the MCP `list_tables` tool lists only reconciled raw sources — the prompt can name tables the agent's own tool omits. `get_metadata` looks up `TenantSchema` by `ctx.schema_name`, which for multi-tenant is the `ws_*` view schema and never matches → returns `table_count=0`.

6. **`TenantMetadata` is per-membership, read three ways.** The materializer writes it to the triggering user's `TenantMembership` only (`materializer.py:547-549`, `OneToOneField` to membership at `models.py:279`); the prompt reads **user-scoped** (`base.py:351-353` — nothing for a user who never materialized), while MCP `describe_table`/`get_metadata` and the DRF dictionary read **any-membership** (`server.py:219,283`; `views.py:293`). Same tenant/table, different annotations per user and per surface.

7. **`commcare_sync` fallback in four places.** Pipeline resolution falls back to `registry.get("commcare_sync")` when it fails — including for OCS/Connect tenants — silently using wrong-provider metadata (`base.py:329`, `server.py:83-103`, `views.py:389-395,583-589`), plus two hardcoded literals (`base.py:843`, `materializer.py:1979`). Only `server.py:_resolve_pipeline_config` is factored.

8. **dbt-model listing is fail-open.** `pipeline_list_tables` drops sources whose physical table is absent (fail-closed) but lists dbt models optimistically when the live-table set is empty (`metadata.py:96-98`). Currently moot — **no shipped pipeline YAML declares `transforms`, so `dbt_models == []` for every pipeline** (`pipeline_registry.py:73`) — but latent: a transient DB error yields a phantom-only catalog that instructs re-materialization.

## Goals

- **One status derivation.** A single function computes canonical world-state; the status API, both prompt builders, and the MCP `get_schema_status` tool all consume it. No surface re-derives.
- **One table catalog.** A single catalog service backs the prompt, the MCP `list_tables`/`get_metadata` tools, and the DRF data-dictionary — same source-of-truth, same physical reconciliation, same `stg_*` policy, one metadata read scope.
- **Truthful by construction.** Reconciliation is uniform (fail-closed); a dead-key read can never silently return `[]`; a wrong-provider fallback surfaces an error instead of lying (aligns with #256).
- **A read-model that layers on top of #255, not under it.** #251 owns *derivation and presentation*; #255 owns the *write side* (janitors, locking, run reconciliation). See collisions below.

## Non-goals (explicit)

- **Any write-side robustness owned by #255** — no janitor, no per-tenant advisory lock, no `MaterializationRun` stale-reconciliation, no dependency-graph ownership between tenant/view schemas. #251 reads their state; it does not manage it.
- **The tenancy/permission substrate (#249/#250).** The status/catalog module runs **after** the centralized authorizer (`apps/workspaces/access.py`, per `2026-06-18-tenant-access-refresh-design.md`) has resolved workspace + live-tenant access. It does not re-implement access checks. Treated as a fixed input.
- **Reviving dbt transforms in the pipeline YAMLs.** The dead `dbt_models` loops are noted, not fed; workspace-scope transforms are #267/#241's problem.
- **New observability** (owned by #257, merged): this consolidation makes the existing `last_error` / status surfaces consistent; it does not add alarms.

## Current-state map

### Two state vocabularies (source enums)

- `SchemaState` (`models.py:15-21`), used by both `TenantSchema.state` and `WorkspaceViewSchema.state`: `PROVISIONING, ACTIVE, MATERIALIZING, EXPIRED, TEARDOWN, FAILED`.
- `MaterializationRun.RunState` + `ACTIVE_STATES` (`models.py:62-80`): `STARTED, DISCOVERING, LOADING, TRANSFORMING, COMPLETED, PARTIAL, FAILED, CANCELLED, STALE`; `ACTIVE_STATES = {started, discovering, loading, transforming}`.
- **No `last_synced_at` column exists** on any model — always derived at read-time from `MaterializationRun.completed_at`.

### Status derivations (the "~7")

| # | Site | Substrate | "in-progress" / "ready" predicate |
|---|---|---|---|
| S1 | `_derive_schema_status` `workspace_views.py:84-105` (the shared oracle → `available/provisioning/unavailable/failed`) | TenantSchema.state counts + view_schema.state | single-tenant: ready iff **all** tenant schemas ACTIVE; multi: keyed on view_schema.state only |
| S2 | `WorkspaceListView` / `WorkspaceDetailView` `last_synced_at` `workspace_views.py:163-170, 310-319` | `MaterializationRun.state=COMPLETED` | COMPLETED-only |
| S3 | single-tenant prompt `base.py:287-382` | `TenantSchema ACTIVE\|MATERIALIZING` + `tables[0].materialized_at` | MATERIALIZING branch (dead) |
| S4 | multi-tenant prompt `base.py:392-479` | `WorkspaceViewSchema` + `MaterializationRun ∈ ACTIVE_STATES` + COMPLETED\|PARTIAL | in-progress = `active_run OR vs.state==MATERIALIZING`; ready = `vs.state==ACTIVE` |
| S5 | MCP `get_schema_status` single `server.py:774-819` | `TenantSchema ACTIVE\|MATERIALIZING`; `last_run.result` dead-key read | tables always `[]` |
| S6 | MCP `get_schema_status` multi `server.py:821-881` | `WorkspaceViewSchema ACTIVE\|MATERIALIZING`; FAILED→`SCHEMA_BUILD_FAILED`; `workspace_list_tables` | ACTIVE\|MATERIALIZING |
| S7 | `load_tenant_context` / `load_workspace_context` `context.py:51-139` | tenant: ACTIVE\|MATERIALIZING; view: **ACTIVE only** | routing gate |
| — | jobs poll `jobs_views.py:83-163` (frontend `useWorkspaceJobs.ts`, 3s) | `ThreadJob.state ∈ ACTIVE_STATES` + `MaterializationRun.progress` | **not** SchemaState-driven; separate axis (in-flight *jobs*, not *world-state*) |

Frontend maps `schema_status` → UI state at `frontend/src/api/workspaces.ts:62-95` (`available→ready`, `provisioning→loading`, `unavailable→empty`, `failed→empty`, fallback `last_synced_at != null ? ready : empty`). `RefreshStatusView` (`views.py:511-539`) exists but has no frontend caller; the live signal is `schema_status` on the workspace payload plus the 3s jobs poll.

### Catalog listers (the "5")

Full matrix in Appendix B. Summary: `transformation_aware_list_tables` (prompt), `pipeline_list_tables` (MCP `list_tables`), `workspace_list_tables` (view-schema VIEWs), `get_schema_status` single-tenant (run.result blob), `_sync_pipeline_list_tables` (DRF dictionary). `stg_*` is hidden **only** in the DRF paths (`views.py:416,569`). Physical reconciliation is present for raw sources everywhere except `get_schema_status` single-tenant and the terminal-asset branch of `transformation_aware_list_tables`.

### Boundary reality

The "standalone MCP server" boundary is already bidirectional fiction: `mcp_server.services.metadata` imports `apps.workspaces`/`apps.transformations` models (`metadata.py:16-18`), while `apps.agents`/`apps.workspaces` import the listers back out of `mcp_server` (7 files, Appendix C). Any consolidation must pick a canonical home and make the dependency direction explicit — this is a decision, below.

## Proposed design

### Recommended: a canonical read-model service (Option A), adopted surface-by-surface

Introduce **one module** that is the single source of truth for world-state and catalog, computed on read from the existing substrates. Every surface delegates to it; no surface re-derives. It is a pure read-model — it never writes schema/run state (that stays with the materializer/#255).

**Shape (names illustrative; final in the plan):**

```
world_state.py (NEW)
  @dataclass WorldState:
      status: Literal["available","provisioning","unavailable","failed"]
      in_progress: bool            # from MaterializationRun.state ∈ ACTIVE_STATES — the ONE definition
      last_synced_at: datetime|None
      last_error: str|None         # from WorkspaceViewSchema.last_error / run failure
      is_multi_tenant: bool
  async def derive_world_state(workspace) -> WorldState        # replaces S1–S7's status logic

catalog.py (NEW)
  @dataclass CatalogTable: name, type, logical_name, row_count, materialized_at, verified: bool
  async def list_catalog(context) -> list[CatalogTable]        # replaces the 5 listers
  async def describe(context, table) -> TableDescription       # one column/metadata path
      # ONE TenantMetadata read scope (below); ONE stg_* policy; uniform fail-closed reconciliation
```

- **`in_progress` has one definition:** `MaterializationRun.state ∈ ACTIVE_STATES` for the workspace's tenants (plus the view-schema rebuild window). The dead `SchemaState.MATERIALIZING` arms are deleted (see Decision 2).
- **`last_synced_at` has one definition** (see Decision 3).
- **Catalog reconciliation is uniform and fail-closed** for both sources and dbt models (Decision 6); the dead-key `run.result["tables"]` read is deleted — single-tenant catalog comes from the same reconciled path as multi-tenant.
- **One `stg_*` policy** across prompt + tools + dictionary (Decision 4).
- **One `TenantMetadata` read scope** (Decision 5).
- **Pipeline resolution** goes through the single factored `_resolve_pipeline_config`, and the `commcare_sync` fallback becomes a truthful error, not a silent wrong-provider substitution (Decision 7, aligns with #256).

**Home + direction (Decision 1):** the module lives in `apps/` (models live there; it is async Django ORM) and `mcp_server` imports it — formalizing the direction that already exists de-facto and removing `mcp_server.services.metadata`'s reverse import of `apps` models over time.

**Why this option:** lowest blast radius that actually closes the divergence — it removes duplication by *substitution* (each surface swaps its bespoke logic for a call), so each phase is independently shippable and testable against the surface it touches, and it never contends with #255's write paths (it only reads run/schema rows).

### Rejected alternatives

- **Option B — denormalized status columns** (write `status` + `last_synced_at` onto `Workspace`/schema rows at each transition). Reads get trivially cheap and consistent, but every writer must be updated in lockstep — and those writers are exactly the materializer/janitor/teardown paths **#255 is actively reworking**. This creates a direct collision (two windows editing the same transition sites) and re-introduces the "15 readers of a state nobody reliably writes" failure mode if a transition is missed. Rejected: couples a read concern to #255's write surface.
- **Option C — event-sourced WorldState projection table** updated by a listener on run/schema events. Cleanest long-term separation, but it needs an event bus we don't have, a backfill, and its own reconciliation (a missed event = stale projection), which overlaps #255's janitor ownership. Over-engineered for a platform with one worker; revisit only if the read-model service proves too slow. Rejected: cost/complexity far exceeds the problem.

## Data-model / migration implications

Option A is **almost entirely code** — it moves logic into two modules and repoints callers. Specifically:

- **No new tables** for status/catalog derivation itself.
- **`SchemaState.MATERIALIZING` removal (Decision 2):** if we drop the enum value, a data migration must map any stray `MATERIALIZING` rows (none expected in prod; grep shows only test fixtures) to `PROVISIONING` or `ACTIVE`. Lower-risk alternative: keep the enum member, delete only the dead **reader** arms — no migration. Recommended: keep the member, delete the readers (avoids a migration and any collision with a future #255 use).
- **`TenantMetadata` scope (Decision 5):** making metadata canonical per-tenant (rather than per-membership) is the one change that *could* need a migration (collapse N membership rows → 1 tenant row). The zero-migration path is to keep the model per-membership but define a single deterministic read (e.g. "most-recently-discovered live membership"), respecting #249's archived-membership filter. Recommended: zero-migration read-rule first; per-tenant model as a follow-up.

## Phased implementation plan

Each phase is independently shippable, independently testable, and leaves the tree green. Ordering puts the pure-derivation consolidation first (no behavior change intended) and the behavior-changing policy decisions in later, separately-reviewable phases.

**Phase 1 — `world_state.derive_world_state` + adopt in the status API.** Extract S1/S2 into the canonical function; `WorkspaceListView`/`WorkspaceDetailView` call it. Golden-master test: for a matrix of (tenant states × run states × view-schema states) the emitted `schema_status`/`last_synced_at` equal today's — except the intentional `last_synced_at` fix (Decision 3), which gets its own asserted test. No prompt/MCP change yet.

**Phase 2 — adopt `derive_world_state` in both prompt builders and the MCP `get_schema_status` tool.** S3–S7 delegate. This is where the divergences (1)(3)(4) close: the prompt, the tool, and the API now tell one story. Delete the dead-key `run.result["tables"]` read; single-tenant catalog comes from Phase 3's path (interim: from the reconciled sources). Contract test: `get_schema_status`, the prompt block, and the status API agree across the rebuild window and the PARTIAL-run case.

**Phase 3 — `catalog.list_catalog` + `catalog.describe`; adopt in MCP `list_tables`/`get_metadata`, the prompt lister, and the DRF dictionary.** One reconciled, fail-closed catalog; one `stg_*` policy (Decision 4); fix `get_metadata`'s `ws_*` lookup miss. This is the #190-class fix: `list_tables` and `describe_table` can no longer contradict. Regression test reproducing the #190 sequence (list says X exists → describe must not say NOT_FOUND for the same catalog).

**Phase 4 — one `TenantMetadata` read scope (Decision 5) + kill the `commcare_sync` silent fallback (Decision 7).** Column annotations become surface-independent and user-independent; wrong-provider resolution surfaces a truthful error. Test: same tenant/table yields identical annotations from prompt, MCP, and dictionary; an unresolvable pipeline returns an error envelope, not commcare metadata.

**Phase 5 (optional cleanup) — dbt fail-closed (Decision 6) + retire the dead `dbt_models` loops** or leave them behind a fail-closed reconciliation so a future YAML with `transforms` is safe by construction. Lowest urgency (currently dead code).

## Test strategy

- **Golden-master before/after** for `derive_world_state` (Phase 1) over the full state matrix — the safety net proving "consolidation ≠ behavior change" except where a decision intentionally changes it.
- **Cross-surface agreement tests** (Phases 2–4): parametrized fixtures assert the status API, the prompt block, the MCP tool, and the DRF dictionary return the same world-state / catalog / annotations for the same workspace. These are the tests whose *absence* let the 7-way divergence accumulate.
- **#190 regression:** the contradictory-catalog scenario (list_tables lists a table the same-context describe_table 404s) must be impossible by construction.
- **Failure-mode tests:** transient `information_schema` error → fail-closed empty (never phantom-listed); unresolvable provider → truthful error (never silent commcare_sync).
- Async DB tests use `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)` per `CLAUDE.md`.

## Cross-cutting collisions

- **#255 (in-flight, background robustness).** #251 is a *read* consolidation; #255 owns the *writes*. Explicit contract: **#251 never writes `SchemaState`/`RunState` and never adds a janitor or lock.** The one shared surface is the **representation of "in-progress."** #251 standardizes reads on `MaterializationRun.state ∈ ACTIVE_STATES`. If #255's per-tenant mutual-exclusion (finding 03#3) wants a written in-progress signal, it should use a Postgres **advisory lock** or its own run-state, **not** revive `SchemaState.MATERIALIZING` — otherwise it re-creates the dead-state divergence #251 just removed. **Flag for the two windows to agree on before either lands** (Decision 2). Also: #255's cascade-FAILED honesty (07#9) already partly shipped via #256 (`WorkspaceViewSchema.last_error`, `state:failed`); `derive_world_state` consumes `last_error` and must not regress it.
- **#249/#250 (tenancy/permission, live window — fixed input).** The catalog/status module runs **after** `resolve_workspace_access` (`apps/workspaces/access.py`). It must not re-implement the membership check, and — critically for Decision 5 — its `TenantMetadata` read must honor the **live-membership** filter (archived `TenantMembership` rows are tombstones; reading annotations from an archived member's row would leak revoked-tenant context into a prompt). The canonical metadata read-rule must filter `archived_at IS NULL`.
- **#252/#255/#261 batch (in-flight).** No file-level collision expected beyond the `MATERIALIZING`/in-progress representation above; the catalog service reads `MaterializationRun.result`/`.progress` which the batch may reshape — Phase 3 should depend on the *reconciled* live catalog, not the `run.result` blob shape, precisely so a `result`-shape change (theirs) can't re-break the catalog (ours).

## Decisions needed from Brian

1. **Canonical module home + dependency direction.** Recommend: `apps/workspaces/services/{world_state,catalog}.py`, with `mcp_server` importing from `apps` (formalizes today's de-facto direction; lets us delete `mcp_server.services.metadata`'s reverse import of `apps` models over time). Alternative: keep the shared logic in `mcp_server.services` (apps already import it) if you want to preserve MCP-as-library. *Recommendation: apps-owned.*
2. **How is "materialization in progress" represented?** Recommend: standardize all reads on `MaterializationRun.state ∈ ACTIVE_STATES`; **keep** the `SchemaState.MATERIALIZING` enum member but **delete its 15 dead reader arms** (no migration). This must be agreed with the #255 window — if #255 needs a *written* in-progress signal for per-tenant locking, it should use an advisory lock, not `MATERIALIZING`. *Recommendation: ACTIVE_STATES + delete dead readers; #255 uses advisory lock.*
3. **`last_synced_at` semantics.** Recommend: `COMPLETED|PARTIAL` (a PARTIAL run *did* load data — matching the prompt's "loaded and ready"), so the UI stops showing `null` for perpetually-PARTIAL tenants. Alternative: keep COMPLETED-only and change the prompt to match. *Recommendation: COMPLETED|PARTIAL everywhere.*
4. **`stg_*` visibility policy — the real product question.** Today the DRF dictionary hides `stg_*` while the agent prompt advertises terminal `stg_*` assets. Should the agent see staging/terminal transformation assets as queryable tables? Options: (a) show terminal assets, hide intermediate `stg_*`; (b) hide all `stg_*` from every surface; (c) show everything. This depends on whether transforms are a product feature (ties to #267/#241). *Recommendation: (a), and reconcile with #267.*
5. **`TenantMetadata` read scope.** Recommend: one deterministic read — "most-recently-discovered **live** membership for the tenant" (respects #249 archived filter), zero migration. Follow-up: promote metadata to per-tenant (one row) with a migration. *Recommendation: deterministic live-membership read now; per-tenant model later.*
6. **dbt-model reconciliation.** Recommend: make dbt-model listing fail-closed like sources (drop the optimistic branch), so the currently-dead loops are safe if a pipeline ever declares `transforms`. Low urgency. *Recommendation: fail-closed.*
7. **`commcare_sync` fallback.** Recommend: remove the silent wrong-provider fallback at all four sites; an unresolvable pipeline returns a truthful error (per #256), not commcare metadata for an OCS/Connect tenant. *Recommendation: truthful error.*

---

## Appendix A — `SchemaState.MATERIALIZING` reader census (15 production sites, 0 writers)

`base.py:298,318,411`; `workspace_views.py:130,293`; `workspace_service.py:88,97,102`; `schema_manager.py:78,113`; `views.py:34,53`; `context.py:66`; `server.py:778,824`. Writers appear only in test fixtures (`test_materialize_workspace_task.py:376`, `test_workspace_service.py:133`, `test_agent_graph.py:129`, `test_schema_context.py:52,307`).

## Appendix B — catalog lister divergence matrix

| Path | file:line | Source | Physical check | `stg_` hidden | sync/async | TenantMetadata scope |
|---|---|---|---|---|---|---|
| `transformation_aware_list_tables` | `metadata.py:269` | Run + TransformationAsset | raw: yes / assets: **no** | no | async | via `base.py` user-scoped |
| `pipeline_list_tables` | `metadata.py:29` | `Run.result[sources]` + dbt_models | sources fail-closed / dbt fail-open | no | async | n/a |
| `workspace_list_tables` | `metadata.py:161` | `information_schema` VIEWs | inherent | n/a | async | n/a |
| `get_schema_status` (single) | `server.py:799` | `Run.result` blob (dead key) | **none** → `[]` | no | async | n/a |
| `_sync_pipeline_list_tables` | `views.py:148` | `Run.result` + dbt_models | sources fail-closed / dbt fail-open | caller filters | sync | n/a |
| `DataDictionaryView` | `views.py:413` | `_sync_pipeline_list_tables` | via live set | **yes (416)** | sync | any-membership |
| `TableDetailView` | `views.py:567` | `_sync_pipeline_list_tables` | via live set | **yes (569)** | sync | any-membership |
| `describe_table`/`get_metadata` | `server.py:219/283` | `information_schema.columns` | inherent | no | async | any-membership |

## Appendix C — module-boundary imports (`apps/*` → `mcp_server` internals)

`apps/agents/graph/base.py:38-45` (context, pipeline_registry, metadata×4); `apps/agents/mcp_client.py:19` (auth); `apps/workspaces/api/views.py:25` (pipeline_registry); `apps/workspaces/tasks.py:40-42` (loaders, pipeline_registry, materializer); `apps/artifacts/views.py:27-28` (context, query); `apps/transformations/services/executor.py:28` (dbt_runner); `apps/users/services/credential_resolver.py:17` (envelope). Reverse: `mcp_server/services/metadata.py:16-18` imports `apps.workspaces`/`apps.transformations` models.
