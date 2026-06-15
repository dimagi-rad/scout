# Seam review: Platform DB ↔ Managed DB

*Reviewer mandate: own the contract between Django state rows (`TenantSchema`,
`WorkspaceViewSchema`, `MaterializationRun`) and physical schemas in the managed
database — every place they can disagree (drops, renames, failed DDL, janitor races)
and who reconciles each. Core question: is the dependency graph between tenant
schemas and view schemas owned anywhere?*

Repo: `/Users/bderenzi/Code/dimagi/scout`, HEAD `35e4230` (post incident-fix PRs #227–#232).

---

## The contract, as enumerated

The implicit contract has four clauses. For each, who enforces it today:

| Clause | Enforced by | Holes |
|---|---|---|
| **C1.** A row in ACTIVE state ⇒ a physical schema with the row's `schema_name` exists and contains the data the latest COMPLETED/PARTIAL run describes | `provision()._ensure_physical_schema` (recreates empty schema only); catalog reconciliation in `pipeline_list_tables` (#185) | MCP `teardown_schema` tool drops physical without touching rows (F2); refresh path activates an empty schema (F1) |
| **C2.** A physical schema with no ACTIVE row ⇒ eventually dropped | nothing (no sweep of managed-DB schemas against rows) | orphan physical schemas persist silently; only observed cost is storage |
| **C3.** Dropping/recreating tenant tables ⇒ dependent `ws_*` views reconciled (rebuilt or row flipped FAILED) | hand-placed hooks: `materialize_workspace` (`build_view_schema` + `_rebuild_sibling_view_schemas`), `teardown_schema` (`_fail_dependent_view_schemas`), `workspace_service.add/remove_workspace_tenant` | 4 mutation paths have **no hook**: legacy `/refresh/` (F1), MCP `teardown_schema` tool (F2), `/api/transformations/runs/trigger/` dbt run (F7), `purge_synced_data` (F10) |
| **C4.** Active use of a workspace ⇒ everything its queries physically depend on stays inside the TTL | `touch_workspace_schemas` — called from exactly one place (chat view) | MCP context, artifacts, recipes, transformations each touch a different, incomplete subset (F4, F5) |

**Answer to the mandate question: the dependency graph is not owned anywhere.**
The platform side of the graph is the `WorkspaceTenant` junction; the physical side
is PostgreSQL's `pg_depend` (acting via `DROP ... CASCADE`). Reconciliation between
them is distributed across at least six hand-maintained hooks, and four live mutation
paths forgot at least one hook each. Every fix in the #227–#230 cluster added another
hook at another site rather than a choke point; the next DDL-touching feature must
remember all of C1–C4 by hand, and history (this report) shows that does not happen.

---

## Findings

### F1 — Legacy `/refresh/` loads fresh data into the old schema, then destroys it; an empty schema goes ACTIVE
**Status:** BROKEN-NOW · **Impact:** data-loss (materialized data; recoverable by re-sync) · **Confidence:** verified-by-trace · **Complexity:** accidental (contract drift: `run_pipeline` grew self-provisioning; the refresh caller was never migrated)

This replicates the v1 run-A S1 finding; it is still present at HEAD.

Chain:
1. UI: data-dictionary refresh → `frontend/src/store/dictionarySlice.ts:197` `POST /api/workspaces/${id}/refresh/` → `apps/workspaces/api/urls.py:22`.
2. `apps/workspaces/api/views.py:325-369` (`RefreshSchemaView.post`): `SchemaManager().create_refresh_schema(tenant)` creates a **new** `TenantSchema` row with unique name `{canonical}_r{8hex}` (`schema_manager.py:169-181`), defers `refresh_tenant_schema`.
3. `apps/workspaces/tasks.py:126-200` (`refresh_tenant_schema`): creates the physical `_r` schema (line 150), then `run_pipeline(membership, credential, pipeline_config)` (line 173) — **no schema argument exists in `run_pipeline`'s signature** (`materializer.py:96-102`).
4. `mcp_server/services/materializer.py:183`: `run_pipeline` provisions its own target: `tenant_schema = SchemaManager().provision(tenant_membership.tenant)` → the **canonical** schema (sanitized `external_id`). All loaders drop/recreate `raw_*` tables and write the fresh data **there**.
5. Back in the task: `tasks.py:182-184` marks the **empty** `_r` schema ACTIVE; `tasks.py:188-197` flips every other ACTIVE schema for the tenant — i.e. the canonical schema now holding the fresh load — to TEARDOWN and defers `teardown_schema` with a 30-minute delay.
6. `tasks.py:609-663` → `schema_manager.teardown:194-212`: `DROP SCHEMA ... CASCADE` destroys the just-loaded data; COMPLETED/PARTIAL runs flip STALE (`tasks.py:639-645`); dependent multi-tenant view schemas flip FAILED with **no rebuild deferred** (`tasks.py:653`, and the justifying comment at `tasks.py:647-652` — "the tenant's data is gone, so a rebuild would rightly fail" — is **false on this path**: a new ACTIVE schema exists; comment/logic mismatch).

Net result of clicking "refresh": ~30 minutes later the tenant's data is gone, the
catalog is empty (the ACTIVE `_r` schema has zero MaterializationRuns), and any
multi-tenant workspace sharing the tenant is flipped FAILED with nobody rebuilding it.
Reachable today by any read-write member via the data dictionary UI.

Window detail: during the 30-minute grace, **two** ACTIVE TenantSchema rows exist for
one tenant; `load_tenant_context` (`mcp_server/context.py:56-59`) and
`_resolve_tenant_schema` (`api/views.py:34-37`) pick `.afirst()` under
`ordering = ["-last_accessed_at"]` — the empty `_r` row (touched most recently at
step 5) wins immediately, so the breakage is visible before the drop even happens.

### F2 — MCP `teardown_schema` tool drops physical schemas but never reconciles any state row
**Status:** BROKEN-NOW · **Impact:** correctness (agent-facing state lies; cross-workspace blast radius) · **Confidence:** verified-by-trace · **Complexity:** accidental

`SchemaManager.teardown`/`ateardown` is documented as physical-only: *"callers are
responsible for updating the model state"* (`schema_manager.py:183-192, 474-477`).
The worker-side caller (`tasks.py:teardown_schema`) honours that. The MCP tool does not:

Chain:
1. Agent calls `teardown_schema(confirm=True)` (LLM-wired tool) → `mcp_server/server.py:801-865`.
2. Lines 840-849: `await mgr.ateardown_view_schema(vs)` — physical `ws_*` drop; **no write to `vs.state`**.
3. Lines 852-858: `await mgr.ateardown(ts)` for every TenantSchema of every tenant in the workspace — physical drop + role drop; **no write to `ts.state`, no STALE flip of MaterializationRuns, no `_fail_dependent_view_schemas`**.

Consequences, each traced:
- `TenantSchema` rows remain ACTIVE → `get_schema_status` (`server.py:689-735`) returns `exists=True, state=active` plus a **ghost table list** read from `last_run.result` (run still COMPLETED, never STALE'd). The agent is told data exists immediately after it confirmed the teardown. Contradictory schema responses are exactly the panic-loop class (#190).
- `query` resolves the schema via `load_tenant_context` (row still ACTIVE) and then fails at `SET ROLE` (`services/query.py:44`) because the readonly role was dropped — surfaced as "Schema configuration error / query failed", not "no data".
- `pipeline_list_tables` mostly self-heals via information_schema reconciliation, **except** dbt models: with `live_table_names == set()` the guard `if live_table_names and model_name not in live_table_names` (`metadata.py:97`) is falsy, so dbt models are listed "optimistically" — phantom catalog entries.
- Tenant schemas are shared across workspaces (`tasks.py:338-343` says so explicitly). The tool drops them for **all** workspaces sharing the tenants, cascade-dropping sibling `ws_*` views, and unlike the worker path it never flips those `WorkspaceViewSchema` rows — they keep claiming ACTIVE over empty schemas.
- Later, `provision()._ensure_physical_schema` (`schema_manager.py:74-77, 131-148`) silently recreates an **empty** physical schema under the still-ACTIVE record whose latest run says COMPLETED — C1 violated in the other direction.

### F3 — Teardown tasks never re-check state: provision can resurrect a TEARDOWN row that a queued teardown then drops
**Status:** LATENT · **Impact:** data-loss · **Confidence:** verified-by-trace (code paths); strong-inference (timing windows) · **Complexity:** accidental

The 2026-06-10 fix closed one leg of the janitor triangle (resurrected rows now get
`last_accessed_at` reset — `schema_manager.py:114-122`). The other leg is open:

- `expire_inactive_schemas` flips ACTIVE→TEARDOWN and defers `teardown_schema` (`tasks.py:538-553`).
- A concurrent `provision()` (any materialization touching this tenant, from any worker slot or the MCP-dispatched `materialize_workspace`) misses the TEARDOWN row in its ACTIVE/MATERIALIZING filter (`schema_manager.py:68-71`), hits the unique constraint, re-fetches, falls through (`:86-93`), runs `CREATE SCHEMA IF NOT EXISTS`, sets the row ACTIVE, and the pipeline loads fresh data into it.
- The already-queued `teardown_schema` job then runs with **no guard that the row is still in TEARDOWN** (`tasks.py:609-631` — fetch by id, unconditional `manager.teardown`), drops the freshly materialized schema, STALEs its runs, fails dependent view schemas, marks the row EXPIRED.

Identical hole in `teardown_view_schema_task` (`tasks.py:587-606` — no state re-check)
versus `add_workspace_tenant`'s PROVISIONING flip + rebuild (`workspace_service.py:22-28`).

Realistic windows: (a) the refresh path's **30-minute** delayed teardown (`tasks.py:195-197`)
— any materialization of the same tenant inside that window resurrects-and-loses; (b)
queue backlog after a worker outage (the 06-09 incident shape): TEARDOWN flips and
teardown jobs accumulate as `todo`, then interleave with new materialize jobs across
worker slots on recovery. A one-line CAS (`filter(id=..., state=TEARDOWN)` before
dropping, or re-checking `last_accessed_at` against the cutoff inside the task) closes it.

### F4 — Artifact live queries bypass multi-tenant routing entirely
**Status:** BROKEN-NOW (for multi-tenant workspaces with live artifacts) · **Impact:** correctness · **Confidence:** verified-by-trace · **Complexity:** accidental (single-tenant-era code never migrated to the view-schema contract)

`ArtifactQueryDataView` (`apps/artifacts/views.py:795-800`) resolves
`tenant = await artifact.workspace.tenants.afirst()` and builds the context with
`load_tenant_context(tenant.external_id)` — the **first tenant's raw schema**. But an
artifact authored in a multi-tenant workspace was written by an agent whose queries ran
through `load_workspace_context` → the `ws_*` view schema, so its stored
`source_queries` reference namespaced views (`{prefix}__{table}`,
`schema_manager.py:334`) that do not exist in any tenant schema. Every live-data fetch
for such an artifact errors per-query ("relation does not exist" via
`_classify_error`). It also touches only that first tenant's schema
(`views.py:810-812`), contributing to F5. The two sides of the same seam disagree
about which physical namespace an artifact's SQL is valid in — and nothing records,
on the artifact, which namespace it was authored against (the stored-SQL coupling the
cartography flags in §4).

### F5 — TTL keep-alive of the dependency graph is hand-rolled per call site; only chat does it correctly
**Status:** LATENT · **Impact:** correctness (scheduled destruction of in-use data) · **Confidence:** verified-by-trace · **Complexity:** accidental

`touch_workspace_schemas` (`workspace_service.py:74-110`) exists precisely because
*"multi-tenant chat activity never touches the underlying TenantSchemas directly, so
without this bulk-touch they expire after the TTL and their DROP SCHEMA CASCADE
silently destroys the views inside the still-ACTIVE view schema"* (its own docstring).
It is called from **one** place: `apps/chat/views.py:151`.

Every other live query surface touches a different incomplete subset:
- MCP `load_workspace_context` (the choke point for all agent tool calls): touches **only** the view schema (`context.py:125`), never the constituents.
- Recipes run endpoint: touches only `workspace.tenant` — the **first** tenant (`apps/recipes/api/views.py:116-122`).
- Artifacts query-data: touches only the first tenant (F4).
- Transformations trigger: touches the single `ts` it runs against (`transformations/views.py:146`).

A multi-tenant workspace kept alive by recipes/artifact dashboards alone (no chat)
will have constituent `TenantSchema.last_accessed_at` go stale; after
`SCHEMA_TTL_HOURS = 24` (`config/settings/base.py:345`) the janitor drops them,
CASCADE removes the views, `_fail_dependent_view_schemas` flips the view schema FAILED
— while the workspace is in active use. Chat-driven workspaces are safe only because
the one correct call site happens to be the dominant path. C4 has no owner; five
sites implement five different answers.

### F6 — Tenant schema names are unbounded: the 63-byte truncation class survives one level above the fixed view-name bug
**Status:** LATENT · **Impact:** security/correctness (cross-tenant physical collision) · **Confidence:** mechanism verified-by-trace; live reachability hypothesis (depends on real provider `external_id` lengths) · **Complexity:** accidental

`_sanitize_schema_name` (`schema_manager.py:625-631`) lowercases/strips but **never
bounds length**; `Tenant.external_id` is `CharField(max_length=255)`
(`apps/users/models.py:115-118`). PostgreSQL truncates identifiers (including quoted
ones) to 63 bytes, so for a long `external_id`:
- `CREATE SCHEMA` creates a 63-byte-truncated physical schema while the Django row stores the full name (`TenantSchema.schema_name` is `max_length=255`, unique on the **untruncated** string) — C1 split at birth.
- Two distinct tenants identical in their first 63 sanitized bytes **collide into one physical schema** while holding two distinct ACTIVE rows: cross-tenant data co-residence, and the readonly role (`{schema}_ro`, truncated the same way) grants each tenant's role over the shared schema.
- Every parameterized `information_schema` lookup compares the **full** stored string as data, not as an identifier — `metadata.py:147`, `api/views.py:84-89/120-127`, `schema_manager.py:328-331` — and matches nothing: catalog empty, view-schema build sees zero tables, while `SET search_path` (identifier, truncated server-side) still resolves.

This is the sibling the incident fix didn't cover: `_view_prefix` got the bounded
digest treatment (`schema_manager.py:219-241`, PR #227), the schema-name generator
did not, nor did `create_refresh_schema`'s `{name}_r{8hex}` (`:176`) which appends 10
chars and collides even earlier. CommCare domain names are free-ish text upstream;
Connect/OCS ids are numeric today — so current exposure is provider-dependent, but
the model field invites 255 bytes and nothing between it and `CREATE SCHEMA` says no.

### F7 — Synchronous dbt trigger rebuilds tenant tables with no view-schema reconciliation
**Status:** LATENT · **Impact:** correctness · **Confidence:** verified-by-trace for the missing hook; strong-inference for the dbt DDL detail · **Complexity:** accidental

`POST /api/transformations/runs/trigger/` (`apps/transformations/views.py:121-163`)
runs `run_transformation_pipeline` synchronously against the tenant schema. Models are
`+materialized: "table"` (`apps/transformations/services/dbt_project.py:44`);
dbt-postgres's table materialization replaces the existing relation and drops the old
one **with CASCADE**, which removes every dependent `ws_*` view (the same mechanism
`tasks.py:338-343` documents for `raw_*` reloads). On the materialization path this is
healed by `build_view_schema` + `_rebuild_sibling_view_schemas` (`tasks.py:322-349`);
the transformations endpoint has **no hook** — no rebuild, no FAILED flip. Dependent
`WorkspaceViewSchema` rows stay ACTIVE while their views for the transformed tables
are gone; `list_tables` (information_schema-driven for view schemas) silently shows
fewer tables. Reachable by any authenticated tenant member via the DRF router.

### F8 — `MATERIALIZING` is a dead state: ~15 readers, zero writers
**Status:** DEBT · **Impact:** correctness (misleading status surfaces) / velocity · **Confidence:** verified-by-trace (exhaustive grep: every occurrence is a filter or comparison; no assignment anywhere in `apps/` or `mcp_server/`) · **Complexity:** accidental

`provision()` marks a schema ACTIVE before any data loads (`schema_manager.py:120`);
nothing ever writes MATERIALIZING. Consequently:
- The agent's "a materialization is already in progress, do NOT trigger another" prompt branch (`apps/agents/graph/base.py:230-237`) can never fire from schema state (partially compensated by the `active_run` check at `:325`).
- Status derivations treating PROVISIONING/MATERIALIZING as "in progress" (`workspace_views.py:85, 249`) and all the `state__in=[ACTIVE, MATERIALIZING]` filters carry a phantom state, and a schema mid-first-load is indistinguishable from one with data (state=ACTIVE either way; `get_schema_status` reports `state: active` during the load).

### F9 — Materializer end-of-run save rewinds `last_accessed_at`
**Status:** LATENT · **Impact:** correctness (TTL accounting) · **Confidence:** verified-by-trace · **Complexity:** accidental

`materializer.py:485-486`: `tenant_schema.state = "active"; tenant_schema.save(update_fields=["state", "last_accessed_at"])`
writes the **in-memory** `last_accessed_at` captured at provision time (start of run,
`schema_manager.py:77/121`). A run lasting N hours rewinds the clock by N hours at the
end, clobbering any touches that landed during the run. Same triangle as the 06-10 TTL
incident, smaller magnitude; only matters when run duration approaches the 24h TTL or
when the rewound value crosses the janitor cutoff.

### F10 — `purge_synced_data` orphans WorkspaceViewSchema rows and `ws_*` schemas
**Status:** DEBT (operator command, dev-targeted) · **Impact:** correctness · **Confidence:** verified-by-trace

The command (`apps/workspaces/management/commands/purge_synced_data.py`) tears down
all tenant schemas and deletes `TenantSchema` rows, but never mentions
`WorkspaceViewSchema`: physical `ws_*` schemas survive (emptied of views by CASCADE)
and their rows keep whatever state they had — typically ACTIVE — over hollow schemas.

---

## What's actually fine (verified)

- **`build_view_schema` post-#227 is sound**: bounded deterministic prefixes with digest disambiguation, final-name collision checks that catch ambiguous `__` delimiters, hard error on 63-byte overflow *before any DDL*, full drop-and-recreate idempotency, partial-schema cleanup + `last_error` persistence on failure (`schema_manager.py:243-447`).
- **The failure-surfacing chain post-#229 is wired end-to-end**: `vs.state=FAILED` + `last_error` → resume task inspects the row and tells the agent "do NOT re-run materialization" (`tasks.py:1066-1095`) → `get_schema_status` distinguishes `failed` from `not_provisioned` (`server.py:743-764`) → ThreadJob terminal state carries the error summary (`tasks.py:1241-1248`).
- **Worker-side teardown ordering discipline is well-reasoned**: STALE flips happen only *after* the physical DROP succeeds, and a failed DROP reverts the row to ACTIVE rather than stranding intact data invisible (`tasks.py:516-533, 620-645`); failed teardowns self-heal via the next janitor tick.
- **Catalog reconciliation (#185) works for source tables**: `pipeline_list_tables` excludes non-completed sources and anything absent from `information_schema` (`metadata.py:57-94`), with the documented fail-closed empty-set behaviour (dbt-model edge in F2 excepted).
- **The #228 resurrect fix is in place**: every provision activation path resets `last_accessed_at` with an explicit comment naming the incident (`schema_manager.py:114-122`).
- **The #230 sibling machinery is correct on its own path**: `_fail_dependent_view_schemas` (drop side) and `_rebuild_sibling_view_schemas` (rematerialize side) use a single annotated subquery avoiding the Django filter+count trap, and never block the resume (`tasks.py:398-463, 666-687`).
- **Connection-hygiene `task` decorator** (`config/procrastinate.py`) wraps every workspaces task, with `tests/test_worker_db_resilience.py` present to enforce registration through it (file existence verified; test contents not read).

## Recommendation sketch (one paragraph)

Make `SchemaManager` the choke point it pretends to be: every physical mutation
(provision, teardown, view build, dbt-triggered table replacement) goes through a
method that *also* owns row-state reconciliation, dependent-view reconciliation
(C3), and TTL touching (C4) — instead of each caller remembering a different subset.
Add the missing CAS guard to both teardown tasks (F3), delete or rewrite the refresh
path to call `materialize_workspace` (F1 — the modern path already does everything
refresh wants), make the MCP teardown tool delegate to the worker-side task (F2),
bound `_sanitize_schema_name` with the same digest scheme as `_view_prefix` (F6), and
either write MATERIALIZING somewhere or delete the state (F8).

## Coverage log

**Deep-read (line-by-line):**
`apps/workspaces/models.py`, `apps/workspaces/services/schema_manager.py` (all 632 lines),
`apps/workspaces/tasks.py` (all 1,289 lines), `apps/workspaces/services/workspace_service.py`,
`apps/workspaces/api/views.py` (all 541 lines), `apps/workspaces/api/materialization_views.py`,
`mcp_server/context.py`, `mcp_server/services/query.py`, `mcp_server/services/metadata.py`,
`mcp_server/services/materializer.py` lines 1–510 (run_pipeline core) + structural grep of writers,
`mcp_server/server.py` lines 71–180 and 496–866 (context resolution, run_materialization,
get_schema_status, teardown_schema), `apps/artifacts/views.py:740-860`,
`apps/recipes/api/views.py:95-140`, `apps/transformations/views.py:100-166`,
`apps/workspaces/management/commands/purge_synced_data.py`, `config/procrastinate.py`,
`apps/agents/graph/base.py:205-240`.

**Skimmed (targeted grep / partial read):**
`apps/users/models.py` (Tenant fields only), `apps/transformations/services/executor.py` (head)
and `dbt_project.py` (materialization config grep), `mcp_server/server.py` other tools
(list_tables read; describe_table/get_metadata/query/cancel not), frontend
(`dictionarySlice.ts` refresh call only), `apps/workspaces/api/workspace_views.py`
(call-site greps only), pipeline YAMLs (not opened; inferred via registry usage).

**Not examined (honest gaps in/adjacent to this seam):**
- `mcp_server/services/materializer.py` lines 510–1972 — the per-table writer functions and cursor/watermark logic (another reviewer's vertical, but they hold DDL that touches C3).
- `mcp_server/services/dbt_runner.py` and actual dbt-postgres DDL verification — F7's CASCADE claim rests on dbt/PG semantics knowledge, not an observed run.
- `apps/workspaces/api/jobs_views.py`, `jobs_cancel.py` — status derivation readers of the shared rows.
- `apps/workspaces/api/workspace_views.py` in full (tenant add/remove endpoints beyond their service calls; status aggregation at :85/:249).
- `backfill_readonly_roles` management command.
- `mcp_server/services/sql_validator.py`, `envelope.py`, `auth.py`.
- All of `tests/` (including whether any test pins the refresh path's broken behavior, and `test_worker_db_resilience.py` contents).
- LangGraph checkpointer schema references (stored SQL in checkpoints vs schema renames).
- `apps/chat/views.py` beyond the `touch_workspace_schemas` call site; agent prompt assembly beyond lines 205–240.
- Live verification of any finding against a running database (static trace only; F1/F2/F3 would all benefit from a reproduction).
- Provider-side `external_id` length distributions (bounds F6's real-world reachability).
