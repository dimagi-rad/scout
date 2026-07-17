# Lens review: Data integrity & state machines

*Reviewer: cross-cutting lens "data integrity" (races, CAS gaps, multi-writer rows,
janitor interactions, partial-failure states, cross-tenant wiring).
Date: 2026-06-12. HEAD: 35e4230. Report-only; no code changed.*

Method: traced every non-test writer of `TenantSchema.state`, `MaterializationRun.state`,
`ThreadJob.state`, `WorkspaceViewSchema.state`, and `last_accessed_at`, plus the physical-DDL
writers in `schema_manager.py` and the materializer, and the consumers that derive routing
or user-facing truth from those columns.

---

## Findings

### F1 — `/refresh/` loads data into the live schema, then schedules that schema's destruction (BROKEN-NOW, data-loss)

**Confidence: verified-by-trace. Complexity: accidental. Replicates v1 run A's S1.**

The refresh feature creates a *new* uniquely-named schema, but the pipeline it invokes
ignores it and loads into the tenant's *existing* schema — which the refresh task then
tears down as "old".

Chain (every hop quoted by location):

1. **Entry**: `POST /api/workspaces/<id>/refresh/` → `RefreshSchemaView.post`
   (`apps/workspaces/api/urls.py:22`, `apps/workspaces/api/views.py:314-370`).
   Reachable from the live UI: `frontend/src/store/dictionarySlice.ts:197`
   (`await api.post(\`/api/workspaces/${activeDomainId}/refresh/\`)`).
2. `create_refresh_schema` creates `TenantSchema(schema_name=f"{base}_r{uuid4.hex[:8]}",
   state=PROVISIONING)` (`apps/workspaces/services/schema_manager.py:169-181`), then
   `refresh_tenant_schema.defer(...)` (`api/views.py:362-365`).
3. Task `refresh_tenant_schema` creates the physical `_r` schema
   (`apps/workspaces/tasks.py:150`) and calls
   `run_pipeline(membership, credential, pipeline_config)` (`tasks.py:173`) — note it does
   **not pass the new schema**; `run_pipeline` takes no schema argument.
4. `run_pipeline` provisions its own target:
   `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`
   (`mcp_server/services/materializer.py:183`). `provision()` computes
   `schema_name = self._sanitize_schema_name(tenant.external_id)` — the **base** name,
   never the `_r` name — and returns/activates the tenant's existing base schema
   (`schema_manager.py:66-129`). All data loads there; at completion the base schema is
   saved ACTIVE (`materializer.py:485-486`).
5. Back in the task, the **empty** `_r` schema is marked ACTIVE
   (`tasks.py:182-184`), and *every other ACTIVE schema for the tenant* — i.e. the base
   schema that just received the data — is flipped to TEARDOWN with a 30-minute delayed
   `teardown_schema` (`tasks.py:188-197`).
6. `teardown_schema` drops the base schema (`tasks.py:609-663` →
   `schema_manager.teardown`, `DROP SCHEMA ... CASCADE` at `schema_manager.py:198-201`)
   and flips its COMPLETED/PARTIAL runs to STALE (`tasks.py:639-645`).

Consequence: 30 minutes after a "successful" refresh, all materialized data for the
tenant is destroyed. The tenant is left with an ACTIVE but physically-empty `_r` schema
that has **zero MaterializationRun rows** (the runs were attached to the dropped base
schema's row), so `pipeline_list_tables` returns nothing and the agent/data-dictionary
see an empty workspace. `RefreshStatusView` (`api/views.py:373-402`) reads the latest
`TenantSchema` row (the `_r` one, state=active) and reports success.

Secondary damage: the orphan `_r` row stays ACTIVE forever and competes with the
re-materialized base schema in `load_tenant_context`'s
`filter(tenant__external_id=...).afirst()` (ordering `-last_accessed_at`,
`mcp_server/context.py:56-66`, `apps/workspaces/models.py:43`), so post-recovery query
routing can land on the empty schema.

Data is recoverable by re-materializing from the provider, but anything derived from
the old schema (and 30 minutes of user confusion per refresh) is lost. This is the
single worst data-integrity defect found.

---

### F2 — Schema names are derived from `external_id` alone; cross-provider ID collision merges two tenants into one schema (LATENT, security / cross-tenant exposure)

**Confidence: verified-by-trace at the code level; occurrence of a real collision is
strong-inference. Complexity: accidental.**

- `Tenant` is unique on `(provider, external_id)` (`apps/users/models.py:124`) — the
  same `external_id` string can legally exist for two providers.
- Connect tenants store `external_id=str(opp["id"])` and OCS tenants
  `external_id=str(exp["id"])` (`apps/users/services/tenant_resolution.py:101,158`) —
  both are plain provider-local **integer sequences**.
- `_sanitize_schema_name` maps any digit-leading id to `t_<n>`
  (`schema_manager.py:625-631`): Connect opportunity 42 and OCS experiment 42 both
  become schema `t_42`.
- `provision()` looks up the schema **by name only** and returns the existing row
  without checking that `existing.tenant_id` matches the requesting tenant
  (`schema_manager.py:68-79`). The two tenants now share one physical schema and one
  `TenantSchema` row owned by whichever provisioned first.
- The loaders use disjoint table names (`raw_visits…` vs `raw_sessions…`), so both
  datasets coexist in the shared schema, and the MCP `query` tool's `search_path` is
  the whole schema (`mcp_server/context.py:159`): **each tenant's users can query the
  other organization's data.**
- `load_tenant_context` filters `tenant__external_id=tenant_id` with no provider
  predicate (`context.py:56-59`), so both workspaces happily resolve the shared row.

Sanitization is also lossy within a provider (`-` → `_`, dots stripped), so e.g.
`"abc-1"` and `"abc.1"`-shaped ids would collide too, though CommCare domain naming
rules likely prevent the intra-provider case. The cross-provider integer collision is
the realistic vector and becomes near-certain as both integer sequences grow.

Fix shape: include the provider in the schema name derivation, and make `provision()`
assert `existing.tenant_id == tenant.id` (collision should be a hard error, not silent
adoption).

---

### F3 — MCP `teardown_schema` tool drops physical schemas but writes no Django state (BROKEN-NOW, correctness)

**Confidence: verified-by-trace. Complexity: accidental (the worker-task twin does all
of this correctly).**

The agent-reachable tool (`teardown_schema` is in `MCP_TOOL_NAMES`,
`apps/agents/graph/base.py:65-76`) at `mcp_server/server.py:801-865` calls
`mgr.ateardown_view_schema(vs)` (`server.py:848`) and `mgr.ateardown(ts)`
(`server.py:857`). Both helpers perform only the physical `DROP SCHEMA ... CASCADE`
("callers are responsible for updating the model state",
`schema_manager.py:183-192,474-512`) — and the tool never updates anything:

- `TenantSchema` rows remain **ACTIVE** pointing at dropped schemas.
- `WorkspaceViewSchema` row remains in its prior state (ACTIVE) pointing at a dropped
  `ws_*` schema, so multi-tenant `load_workspace_context` still routes to it
  (`context.py:113-125`).
- `MaterializationRun` rows remain COMPLETED — the worker-task path flips them STALE
  precisely so the catalog stops advertising ghost tables (`tasks.py:633-645`).
- Sibling multi-tenant workspaces sharing the dropped tenant schemas had their views
  cascade-dropped, but `_fail_dependent_view_schemas` (`tasks.py:666-687`) is never
  invoked, so their rows stay ACTIVE-but-empty — the exact lie PR #230 fixed on the
  task path.

Partial mitigations that keep this from being worse: `pipeline_list_tables` reconciles
against live `information_schema` (#185, `mcp_server/services/metadata.py:76-84`), so
single-tenant `list_tables` degrades to "no data, run materialization"; and
`provision()._ensure_physical_schema` recreates a dropped-but-ACTIVE schema on the next
run (`schema_manager.py:74-77`). But the state plane and the physical plane disagree
until a full re-materialization happens, sibling workspaces are silently broken, and
`get_schema_status` reports `state=active` for schemas that do not exist.

Also note the blast-radius mismatch: the tool's docstring says "all materialized data
for this workspace", but tenant schemas are shared across workspaces — an agent turn in
workspace A destroys the data workspace B queries.

---

### F4 — Teardown tasks have no state CAS: a queued teardown destroys a schema that provision has since resurrected (LATENT, data-loss)

**Confidence: code paths verified-by-trace; the interleaving is strong-inference.
Complexity: accidental.**

`teardown_schema` (`tasks.py:609-663`) and `teardown_view_schema_task`
(`tasks.py:587-606`) fetch the row by id and unconditionally drop the physical schema —
they never check that the row is still in TEARDOWN.

Race: TTL janitor flips ACTIVE→TEARDOWN and defers teardown (`tasks.py:538-553`).
Before the worker executes it (queue delay — incidents on 2026-05-30 and 2026-06-09
show this can be hours), a user triggers materialization: `provision()` hits the unique
constraint, re-fetches the TEARDOWN row, falls through, recreates the physical schema
and sets ACTIVE + fresh `last_accessed_at` (`schema_manager.py:86-122`). The pipeline
loads data. The stale queued teardown then runs: drops the freshly-loaded schema,
flips its runs STALE, marks the row EXPIRED — `f0ed483` (PR #228) fixed the
*resurrect-without-touch* variant of this incident, but the *stale-queued-teardown*
variant survives. Same for view schemas: a TTL teardown queued behind a
`rebuild_workspace_view_schema` drops the just-rebuilt `ws_*` schema and marks the row
EXPIRED.

Fix shape: in both teardown tasks, re-check `state == TEARDOWN` (CAS) immediately
before the DROP, and skip otherwise — mirroring the CAS discipline the
MaterializationRun writers already follow.

---

### F5 — Recipe execution is dead and leaks RUNNING rows: `RecipeRunner` calls `build_agent_graph` with a signature that no longer exists (BROKEN-NOW, correctness + orphan state)

**Confidence: verified-by-trace. Complexity: accidental (rename residue from the
tenant→workspace migration). Replicates a v1 finding.**

- `POST /api/recipes/<id>/run/` → `RecipeRunner(...).execute()`
  (`apps/recipes/api/views.py:105-108`), no `graph` argument.
- `execute()` first creates `RecipeRun(status=RUNNING)`
  (`apps/recipes/services/runner.py:189` → `:123-143`), **then** builds the graph
  *outside* the try/except: `_build_graph` calls
  `build_agent_graph(tenant_membership=..., user=..., checkpointer=None)`
  (`runner.py:115-119`).
- The actual signature is `build_agent_graph(workspace, user=None, checkpointer=None,
  mcp_tools=None, oauth_tokens=None)` (`apps/agents/graph/base.py:480-486`) →
  `TypeError: unexpected keyword argument 'tenant_membership'` on every call.
- The view's `except Exception` returns 500 (`api/views.py:109-111`); the RecipeRun row
  is **left in RUNNING forever** — no janitor reconciles RecipeRun (unlike ThreadJob).

Even if the keyword were fixed, the initial state uses pre-migration keys
(`tenant_id`, `tenant_name`, `tenant_membership_id`, no `workspace_id`/`thread_id`,
`runner.py:215-224,302-311`) while the graph injects `state["workspace_id"]` into every
MCP call (`graph/base.py:504-508`), so tools would all fail validation. Two
independent breaks = this path has not worked since the workspace migration; its only
green tests must inject a fake graph.

---

### F6 — Multi-tenant artifacts execute their stored SQL against one arbitrary tenant's schema (BROKEN-NOW for multi-tenant workspaces, correctness / silent wrong data)

**Confidence: verified-by-trace. Complexity: accidental.**

`ArtifactQueryDataView.get` resolves the execution context as
`tenant = await artifact.workspace.tenants.afirst()` then
`ctx = await load_tenant_context(tenant.external_id)`
(`apps/artifacts/views.py:795-800`) — never `load_workspace_context`.

For a multi-tenant workspace, the agent authored `source_queries` against the
**view schema** (namespaced `prefix__table` views) because that is what `query` ran
against during the conversation (`context.py:113-139`). Re-executing those queries in
the first tenant's `t_*` schema either errors (view names don't exist there) — the
"good" outcome — or, if the SQL happens to reference raw table names that exist in a
tenant schema, **silently returns one tenant's slice as if it were the workspace
union**. `tenants.afirst()` ordering is `Tenant.Meta.ordering = ["canonical_name"]`,
i.e. which tenant gets picked depends on alphabetical accident.

Every live/refreshable artifact in a multi-tenant workspace is affected
(`/artifacts/<id>/query-data/` is the sandbox's live-data path). Fix shape: use
`load_workspace_context(workspace_id)` exactly like the MCP `query` tool.

---

### F7 — Partial multi-tenant materialization leaves the workspace's own view schema ACTIVE but missing the re-materialized tenant's views (LATENT, correctness)

**Confidence: strong-inference (each step verified; combined run not executed).
Complexity: accidental.**

In `materialize_workspace`, every per-tenant load DROPs and recreates that tenant's
`raw_*` tables with CASCADE (e.g. `materializer.py:1419,1514`), which destroys the
namespaced views inside the **current** workspace's `ws_*` schema. The rebuild that
would recreate them only runs when *all* tenants succeeded:
`if workspace_tenant_count > 1 and all_succeeded:` (`tasks.py:320-336`). The sibling
rebuild explicitly excludes the current workspace
(`_sibling_view_schema_workspaces(...).exclude(id=exclude_workspace_id)`,
`tasks.py:429-438`).

So when tenant A reloads fine and tenant B fails (or a cancel breaks the loop at
`tasks.py:285-288`), the workspace's `WorkspaceViewSchema` stays **ACTIVE** while A's
views are gone. The resume prompt then tells the agent A's sources are `completed`
(`_aggregate_materialization_state`, `tasks.py:928-1016`) and the view-schema health
check passes (`vs.state == ACTIVE`, `tasks.py:1075-1083`), but `list_tables` through
the view schema shows only B's stale views — A's fresh data is unreachable, and the
agent gets exactly the contradictory catalog that previously produced panic loops
(#190). Fix shape: attempt the rebuild (or at minimum flip the vs row FAILED) whenever
*any* tenant was re-materialized, not only on `all_succeeded`.

---

### F8 — Two cancel vocabularies: MCP `cancel_materialization` writes FAILED, everything else writes CANCELLED (LATENT today, correctness)

**Confidence: verified-by-trace. Complexity: accidental.**

The worker's cancellation checkpoint tests `current_state == CANCELLED`
(`tasks.py:489-494`), and all materializer phase-CAS comments assume CANCELLED is what
a canceller writes (`materializer.py:20-24,236-244`). The API cancel paths comply
(`jobs_cancel.py:30-36`, orphan path `materialization_views.py:100-104`). But the MCP
tool `cancel_materialization` writes `state=FAILED, result={"cancelled": True}`
(`mcp_server/server.py:478-482`). Consequences if invoked mid-LOAD: the page-loop
checkpoint never fires (state isn't CANCELLED), the load continues to the end of the
phase, and the run terminates through the phase-CAS-miss path with inconsistent
state/result semantics; downstream aggregation reports "failed" instead of
"cancelled".

Reachability today: **not** in `MCP_TOOL_NAMES` (`graph/base.py:65-76`), so the agent
cannot call it; only a hypothetical direct MCP client can. Classified LATENT — but it
is the kind of dead-but-live tool that gets re-wired without anyone re-reading its
write semantics. Fix shape: make it write CANCELLED via `cancel_thread_job`-equivalent
logic, or delete the tool.

---

### F9 — Pipeline completion rewrites `last_accessed_at` with a stale pipeline-start value (DEBT, cost/correctness; small re-opening of the #228 incident class)

**Confidence: verified-by-trace. Complexity: accidental.**

`run_pipeline` holds the `TenantSchema` instance from provision time (when
`last_accessed_at` was set to *now*, `schema_manager.py:120-122`) and at completion
saves `update_fields=["state", "last_accessed_at"]` (`materializer.py:485-486`) — i.e.
it writes the **provision-time** timestamp back, clobbering any newer touch made by MCP
activity during the load (`context.py:66,125`). For a pipeline that runs H hours, the
freshly-materialized schema finishes with a TTL clock already H hours old against
`SCHEMA_TTL_HOURS = 24` (`config/settings/base.py:345`). A long load (or a backlogged
worker) plus a quiet user re-creates a small version of the 2026-06-10 "janitor drops
fresh schema" incident. Fix shape: set `last_accessed_at = timezone.now()` explicitly
at completion (as `refresh_tenant_schema` already does at `tasks.py:182-184`).

---

### F10 — No janitor reconciles MaterializationRun rows stuck in ACTIVE states after a hard worker crash (DEBT, correctness)

**Confidence: verified-by-trace (absence). Complexity: accidental.**

A worker that dies mid-LOAD leaves the run in DISCOVERING/LOADING/TRANSFORMING forever:

- `expire_stale_thread_jobs` / `reconcile_stale_thread_job` flip only the **ThreadJob**
  (`tasks.py:728-838`); the MaterializationRun is never touched.
- `expire_inactive_schemas` deliberately doesn't touch runs (`tasks.py:526-533`), and
  `teardown_schema` only re-states COMPLETED/PARTIAL runs (`tasks.py:639-645`).

Consequences of an eternal-LOADING zombie row: (a) `_load_prior_resume_cursors` looks
at the **most recent** prior run and bails unless it is PARTIAL/FAILED
(`materializer.py:583-593`), so a zombie blocks resume from an earlier PARTIAL run and
forces a full reload (correct but wasteful — this same strict design is also what
*prevents* the zombie from causing row duplication, see F12); (b)
`materialization_cancel_view` counts zombies as active runs and reports them
"cancelled" (`materialization_views.py:46-54`); (c) `_aggregate_materialization_state`
classifies them as eternal "partial" (`tasks.py:1012-1015`). This is the
MaterializationRun-side sibling of the 2026-05-30 zombie-`doing`-jobs incident. Fix
shape: extend the janitor to CAS runs in ACTIVE_STATES whose `procrastinate_job_id`
maps to a terminal procrastinate status → FAILED.

---

### F11 — Acknowledged create-order race: ThreadJob row committed after `defer_async` (DEBT, known)

**Confidence: verified-by-trace (it is documented in-code). Complexity: accidental,
acknowledged.**

`run_materialization` defers the job, then creates the ThreadJob
(`mcp_server/server.py:606-635`); the worker hedges with a 0–3.75 s backoff loop and
otherwise abandons the resume to the janitor (`tasks.py:363-396`, TODO at `:373`
admits the fix: pre-create with nullable `procrastinate_job_id`). Failure mode today is
a delayed (≤15 min, janitor tick) follow-up message, not data loss. Reported to keep
the known debt on the books; the CAS discipline downstream (claim PENDING/CANCELLED →
RUNNING at `tasks.py:1043-1047`, terminal CAS on RUNNING at `tasks.py:1264-1271`) is
sound.

---

### F12 — Resumable Connect writers duplicate rows if a stale cursor is ever replayed; safety currently rests on an implicit invariant (hypothesis-level, correctness)

**Confidence: hypothesis (no live path found; documenting the fragile invariant).
Complexity: accidental.**

Five resumable Connect tables (`completed_works`, `payments`, `invoices`,
`assessments`, `completed_modules`) have **synthetic IDENTITY PKs and no natural-key
`ON CONFLICT`** (`materializer.py:1290-1335,1573-1961`); replaying any page re-inserts
silently (the f26c1a0 bug class). Replay is prevented only because:

1. the per-page watermark is persisted *after* `conn.commit()`
   (`materializer.py:1653-1658` pattern) — a crash in that window strands committed
   rows beyond the recorded cursor, **but**
2. a hard crash leaves the run in LOADING, and `_load_prior_resume_cursors` ignores
   non-PARTIAL/FAILED runs (`materializer.py:589-593`), forcing a clean DROP+reload;
   the only writers that flip a run to PARTIAL/FAILED with cursors run in-process with
   an in-memory cursor that matches what was committed (`materializer.py:328-371`).

So correctness depends on "no out-of-process writer ever flips an ACTIVE run to
PARTIAL/FAILED". F10's recommended janitor (flip zombie runs to FAILED) would *break
exactly this invariant* and create a real duplication path — the FAILED zombie's
`result.sources` carries a cursor older than the committed data. If F10 is fixed,
either null out `cursor_state` when the janitor flips, or give these five tables
natural keys / `ON CONFLICT`. `visits` is already safe (`ON CONFLICT (visit_id)`,
`materializer.py:1271`), as are all CommCare/OCS tables (natural PKs + upsert).

---

### F13 — User-merge conflict path cascade-deletes the duplicate's TenantMetadata (DEBT, minor)

**Confidence: verified-by-trace. Complexity: accidental.**

`_merge_tenant_memberships` deletes the duplicate's conflicting memberships
(`apps/users/services/merge.py:166`); `TenantMetadata` is a OneToOne on
`TenantMembership` with CASCADE (`apps/workspaces/models.py:266-270`), so discovered
provider metadata (and the membership's `provider_metadata` team info) riding on the
duplicate's membership is destroyed even when the canonical membership has none. Cheap
to recover (next materialization re-discovers), but the merge silently prefers the
older membership's metadata. Worth a "move metadata before delete" tweak.

---

### F14 — Concurrent materializations of the same tenant are unguarded (documented in-code, unmitigated) (LATENT, correctness)

**Confidence: verified-by-trace (the guard's own comment states it). Complexity:
accidental, acknowledged.**

The dispatch guard is scoped per-thread: "this lets two threads in the same workspace
dispatch parallel materializations that share tenant_schemas … the materializer has no
advisory lock per tenant_schema" (`mcp_server/server.py:580-590`). Two concurrent
`run_pipeline` calls on one tenant interleave `DROP TABLE`/`CREATE`/`INSERT`/per-page
commits on the same physical tables: outcomes range from one run failing on missing
tables to interleaved duplicate rows in the resumable tables (no natural PK, F12).
Also reachable across workspaces sharing a tenant and via `materialization_retry_view`.
Fix shape: pg advisory lock keyed on `tenant_schema.id` around the LOAD phase, or
tenant-scoped dedupe at dispatch.

---

## What's fine (verified healthy)

- **Resume/ThreadJob CAS chain**: claim (PENDING/CANCELLED→RUNNING), terminal CAS
  scoped to RUNNING with re-read on miss, idempotent retries (`tasks.py:1033-1289`).
  The 19-commit fix chain landed in a genuinely sound place.
- **Cancel ordering**: DB flip before procrastinate abort, consistently funneled
  through `cancel_thread_job` (`jobs_cancel.py`), with per-user scoping and a
  careful orphan-vs-other-user distinction (`materialization_views.py:56-117`).
- **Materializer phase transitions**: DISCOVERING→LOADING→TRANSFORMING→COMPLETED are
  all conditional UPDATEs preserving concurrent CANCELLED (`materializer.py:240-244,
  435-443,471-483`); per-source partial-failure bookkeeping is honest (PARTIAL vs
  FAILED via `_has_committed_cursor`).
- **Catalog reconciliation**: `pipeline_list_tables` filters to `state=="completed"`
  sources AND live `information_schema` tables; failure of the live check degrades to
  an empty catalog rather than phantom rows (`metadata.py:29-158`).
- **Janitor status semantics**: `_procrastinate_job_status` returns None on "couldn't
  tell" and callers skip the row, avoiding misclassification during DB blips
  (`tasks.py:693-747`); API-side backstop reconciler covers the dead-worker case
  (`jobs_views.py:117-135`).
- **View-name truncation guard**: bounded prefixes + pre-DDL collision and 63-byte
  checks on final names (`schema_manager.py:219-350`) — the #227 fix is thorough.
- **Post-teardown truth-telling on the task path**: STALE flip after successful DROP
  only, dependent view schemas flipped FAILED, sibling rebuilds deferred
  (`tasks.py:609-687,341-349`).
- **Chat thread binding**: ownership + workspace check before upsert, foreign-thread
  404 (`chat/views.py:116-137`).
- **TTL touch fan-out for multi-tenant workspaces** (`workspace_service.py:74-111`)
  closes the "constituent schemas expire under a live view schema" hole.

## Coverage log

**Deep-read (line-by-line):** `apps/workspaces/tasks.py`, `apps/workspaces/models.py`,
`apps/chat/models.py`, `apps/workspaces/services/schema_manager.py`,
`apps/workspaces/services/workspace_service.py`, `apps/workspaces/api/views.py`,
`apps/workspaces/api/materialization_views.py`, `apps/workspaces/api/jobs_cancel.py`,
`apps/workspaces/api/jobs_views.py`, `apps/workspaces/api/urls.py`,
`mcp_server/server.py`, `mcp_server/context.py`, `mcp_server/services/materializer.py`
(full 1973 lines), `mcp_server/services/metadata.py`, `mcp_server/loaders/connect_base.py`,
`apps/chat/views.py`, `apps/agents/graph/state.py`, `apps/recipes/services/runner.py`,
`apps/users/signals.py`, `mcp_server/services/dbt_runner.py`,
`apps/transformations/services/executor.py`.

**Skimmed (targeted greps / partial reads):** `apps/agents/graph/base.py` (tool-name
set, signature, injection map only), `apps/artifacts/views.py` (query-data view +
outline only), `apps/users/services/merge.py` (outline + data-move calls),
`apps/users/models.py` (User/Tenant/TenantConnection/TenantMembership),
`apps/recipes/api/views.py` (run endpoint only),
`apps/users/services/tenant_resolution.py` (external_id assignment lines only),
`config/settings/base.py` (TTL constant only), `frontend/src/store/dictionarySlice.ts`
(refresh call site only).

**Not examined (in-scope for this lens but unopened):** the 18 remaining loader files
(`commcare_*`, `connect_*` except base, `ocs_*` — pagination ordering assumptions
unverified), `mcp_server/services/query.py` (SET ROLE/search_path execution),
`mcp_server/envelope.py`, `apps/chat/stream.py`, `apps/chat/thread_views.py` (share
tokens, viewed-state writers), `apps/chat/checkpointer.py` and
`apps/agents/memory/checkpointer.py` (checkpoint write concurrency — a known
multi-writer surface I did not trace), `apps/agents/tools/*` (artifact/learning/recipe
tool writers beyond the version grep), `apps/knowledge/*` (TableMetadata free-text-key
writers), `apps/workspaces/api/workspace_views.py` (member/tenant CRUD races),
`apps/workspaces/management/commands/purge_synced_data.py` (a MaterializationRun
writer I never opened), `apps/users/services/credential_resolver.py` /
`token_refresh.py`, `apps/transformations/services/lineage.py` /
`commcare_staging.py`, `config/procrastinate.py` (the temporary connection-hygiene
decorator), all frontend state slices except the one grep, and all of `tests/` (what
the mocks hide is unassessed).
