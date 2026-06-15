# Lens review: Consistency — same problem solved N ways

*Reviewer: cross-cutting lens "consistency" (methodology v2, Phase 1C-5). 2026-06-12.*
*Mandate: hunt one defect class everywhere — the same problem implemented N ways with
diverging semantics. Report only; no code changed.*

For each family below: every implementation is enumerated, semantics compared, and the
divergences that are (or will become) bugs are called out as findings. Evidence
standards: chains quoted per `file:line`; confidence labelled; comments treated as
claims, not facts.

---

## Family A — Workspace/tenant access resolution (7 implementations, 3 incompatible semantic tiers)

| # | Implementation | Checks | Role-aware? | Used by |
|---|---|---|---|---|
| A1 | `apps/workspaces/workspace_resolver.py:12` `resolve_workspace_drf` | WorkspaceMembership exists | no (returns membership, most callers discard it) | workspaces, knowledge, recipes DRF views |
| A2 | `workspace_resolver.py:35` `resolve_workspace` (sync) | same | no | artifacts sync views |
| A3 | `workspace_resolver.py:49` `aresolve_workspace` (async) | same | no | artifacts async, jobs, materialization cancel/retry, thread list |
| A4 | `apps/chat/helpers.py:88` `_resolve_workspace_and_membership` | WorkspaceMembership **+ TenantMembership when single-tenant** | no | chat streaming + thread views |
| A5 | `apps/workspaces/permissions.py:21,28,36` (`IsWorkspaceMember`/`ReadWrite`/`Manager`) | DRF permission classes | yes | **zero callers — dead code** |
| A6 | inline `membership.role != WorkspaceRole.MANAGE` checks | ad-hoc | yes | `workspace_views.py:302,333,390,464,503,554,600`; `api/views.py:330,500`; `transformations/views.py:84,154` |
| A7 | `mcp_server/server.py:496` `_resolve_workspace_memberships` | **TenantMembership only** (no WorkspaceMembership at all) | no | MCP `run_materialization` authz guard (server.py:558) |

Confirmed by grep: the three permission classes in A5 are referenced nowhere outside
their own module (`grep -rn "IsWorkspaceMember\|IsWorkspaceReadWrite\|IsWorkspaceManager" apps/ config/`
returns only `permissions.py` itself). Replicates the v1 "roles unenforced" finding with
the full call map.

**Semantic consequences (verified by reading every caller):**

- A READ-role member can: create/update/delete knowledge entries
  (`knowledge/api/views.py:49–272` — all discard `_membership`), run recipes
  (`recipes/api/views.py:35–150`), patch/delete artifacts (`artifacts/views.py:683–957`
  via A2/A3, role never read), cancel any workspace materialization
  (`materialization_views.py:42`), cancel thread jobs (`jobs_views.py:172`), and
  dispatch a retry materialization (`materialization_views.py:145`). Role checks exist
  only on workspace settings/members (A6), `/refresh/`, transformations.
- "Can this user see this data?" has three different answers by entry path: chat
  requires TenantMembership for single-tenant workspaces (A4), the artifacts/knowledge/
  data-dictionary APIs require only WorkspaceMembership (A1–A3) — yet both reach the
  same tenant data; MCP `run_materialization` requires only TenantMembership (A7) and
  never checks WorkspaceMembership.

**Finding A (DEBT / security, verified-by-trace):** role model exists in the schema,
is enforced in exactly one cluster of endpoints, dead in the DRF-class form, and absent
from every content surface. Accidental complexity: 7 resolvers where 1 parameterized one
would do.

---

## Family B — Cancellation (3 implementations; one contradicts the worker's contract)

| # | Implementation | Terminal state written | Aborts procrastinate job? | Flips ThreadJob? |
|---|---|---|---|---|
| B1 | `apps/workspaces/api/jobs_cancel.py:19` `cancel_thread_job` | `CANCELLED` (runs + ThreadJob) | yes (`jobs_cancel.py:44`, after DB flip — order documented) | yes |
| B2 | `api/materialization_views.py:22` `materialization_cancel_view` | delegates to B1; orphan fallback `CANCELLED` + abort (`:104,108`) | yes | via B1 |
| B3 | MCP tool `cancel_materialization`, `mcp_server/server.py:446` | **`FAILED`** (`server.py:479` `run.state = MaterializationRun.RunState.FAILED`) | **no** | **no** |

The worker's cancellation checkpoint reacts **only** to `CANCELLED`:

```
apps/workspaces/tasks.py:493  if current_state == MaterializationRun.RunState.CANCELLED:
apps/workspaces/tasks.py:494      raise MaterializationCancelled()
```

So a B3 "cancel" does not stop the load: `progress_updater` never raises, the LOAD
phase runs to completion, and only the LOADING→TRANSFORMING CAS
(`materializer.py:435–443`) notices the state mismatch — after all pages are fetched
and committed. The run ends `FAILED` with `result.cancelled=True`; downstream consumers
that branch on `CANCELLED` (resume aggregation `tasks.py:991–1003`, janitor, retry
affordances) classify it as a failure.

**Reachability check:** `cancel_materialization` and `get_materialization_status` are
**not** in `MCP_TOOL_NAMES` (`apps/agents/graph/base.py:65–76`), so the agent never
sees them; only a direct MCP client can hit B3 today.

**Finding B (LATENT / correctness, verified-by-trace):** the MCP cancel tool implements
a third, contradictory cancellation semantic (FAILED, no abort, ThreadJob left ACTIVE).
Either delete it or funnel it through `cancel_thread_job` like B2 was made to.

---

## Family C — Materialization/schema status derivation (7 implementations)

1. `tasks.py:928` `_aggregate_materialization_state` — per-run states folded with
   precedence cancelled > failed > completed > partial; reads `result["sources"]`.
   Source of truth for the resume prompt.
2. `api/jobs_views.py:25` `_job_to_dict` — ThreadJob.state + percent derived from
   `run.progress.rows_loaded/rows_total`; `_termination_to_dict:60` derives
   `retry_available` from FAILED/CANCELLED.
3. `api/views.py:377–398` refresh-status — latest `TenantSchema.state`, mapped to
   `"unavailable"`/state/error string.
4. `api/workspace_views.py:244–274` workspace detail — `_derive_schema_status` from
   counts of ACTIVE TenantSchemas + PROVISIONING|MATERIALIZING existence + view-schema
   state; **`last_synced_at` counts only `COMPLETED` runs** (`workspace_views.py:266–268`).
5. MCP `get_materialization_status` (`server.py:408`) — raw `run.state` + raw `result`.
6. MCP `get_schema_status` (`server.py:652`) — TenantSchema/ViewSchema state + last
   COMPLETED|PARTIAL run; extracts `tables` from `run.result` (see Finding C2).
7. Agent prompt assembly — `graph/base.py:206` `_fetch_schema_context` (single-tenant)
   and `graph/base.py:306` `_fetch_multi_tenant_schema_context`, each its own
   state machine over TenantSchema / WorkspaceViewSchema / MaterializationRun;
   "last updated" uses COMPLETED|PARTIAL (`base.py:357–366`).

**Finding C1 (LATENT / correctness, verified-by-trace for the divergence):**
"when was this workspace last synced" has two definitions: workspace detail counts only
COMPLETED runs (`workspace_views.py:266`), the agent prompt and `get_schema_status`
count COMPLETED|PARTIAL. A tenant whose runs always end PARTIAL (one chronically
failing source) shows `last_synced_at: null` in the UI while the agent says "Data is
loaded and ready. Last updated: …". Same data, opposite story.

**Finding C2 (BROKEN-NOW low-severity / correctness, verified-by-trace):**
`get_schema_status` extracts its `tables` list via
`if "tables" in result_data … elif "table" in result_data and "rows_loaded" in result_data`
(`server.py:718–725`). The materializer writes neither key — `run.result` contains
`sources` and top-level `rows_loaded` only (`materializer.py:368,398,466,503-504`;
grep for `"tables"` in materializer returns nothing). Chain: agent →
`get_schema_status` (in `MCP_TOOL_NAMES`) → single-tenant branch → `tables: []`
always, even right after a successful run. The agent is told `exists: true,
state: active, tables: []` — stale-shape residue from a pre-#198 result format,
mitigated only because the prompt steers the agent to `list_tables`.

---

## Family D — Table catalogs (6 implementations; v1 counted five — it's six)

1. `mcp_server/services/metadata.py:29` `pipeline_list_tables` — pipeline YAML ∩
   `run.result["sources"]` ∩ `information_schema`, with
   `materialized_row_count`/`row_count_verified`.
2. `metadata.py:161` `workspace_list_tables` — raw `information_schema` for view
   schemas; `materialized_row_count: None` always.
3. `metadata.py:269` `transformation_aware_list_tables` — third variant preferring
   terminal dbt models.
4. `apps/workspaces/api/views.py:236–252` data-dictionary endpoint —
   `_get_from_pipeline` plus a **legacy fallback to `workspace.data_dictionary`
   JSONField** (`api/views.py:425–427`).
5. `get_schema_status`'s `tables` (dead keys; Finding C2).
6. Agent prompt context (`graph/base.py`) — picks 1 or 3 depending on transformation
   assets, then renders its own structure.

Plus `knowledge.TableMetadata` keyed by free-text `table_name`, annotating whichever of
the above the user happened to see. These disagree by construction on row counts
(1 vs 2), on table visibility during in-progress loads (1 hides `in_progress` sources;
2 shows whatever exists physically), and on staleness (4's JSON fallback can describe a
schema that no longer exists). No new S1 here beyond C2, but this family is why catalog
reconciliation (#185) had to exist. **DEBT / correctness, verified-by-trace** for the
enumeration.

---

## Family E — Workspace→schema routing: canonical resolver vs. first-tenant shim

The canonical router is `mcp_server/context.py:83` `load_workspace_context`:
1 tenant → tenant schema; 2+ → ACTIVE `WorkspaceViewSchema` (`ws_<hash>`), with the
invariant documented at `context.py:89–94`.

A second, older idiom survives on the Django side: `Workspace.tenant` =
`self.tenants.first()` (`workspaces/models.py:143–146`, explicitly labelled
"Single-tenant compatibility") and direct `tenants.afirst()`:

- Data dictionary: `api/views.py:241,245,479,483,506`
- `/refresh/`: `api/views.py:336` (refreshes an arbitrary tenant of a multi-tenant workspace)
- Refresh status: `api/views.py:387`
- Knowledge tables view: `knowledge/api/views.py:254`
- Recipes: `recipes/api/views.py:116`; recipe runner config `recipes/services/runner.py:217–218`
- **Artifact live queries**: `artifacts/views.py:795–800` (`ArtifactQueryDataView`)

**Finding E (BROKEN-NOW / correctness for multi-tenant workspaces):**

Chain for the worst case — live artifacts:
- Agent in a multi-tenant workspace authors `source_queries` SQL via the MCP `query`
  tool, which runs in the **view schema** with namespaced views
  `{prefix}__{table}` (`context.py:113–131`, `schema_manager.py:335` view naming).
- Frontend renders the artifact → `GET /api/workspaces/<id>/artifacts/<id>/query-data/`
  → `ArtifactQueryDataView.get` (`artifacts/views.py:773`) → `tenants.afirst()`
  (`:795`) → `load_tenant_context(tenant.external_id)` (`:800`) → executes in the
  **first tenant's `t_` schema**, where `prefix__table` views do not exist.
- Result: every live query errors (returned per-query at `:803–807` shape), or — if
  the SQL happens to name a raw table — silently returns one tenant's slice of a
  multi-tenant artifact.

Routing divergence verified-by-trace; the SQL-shape consequence is strong-inference
(depends on agent-authored SQL, which in multi-tenant context can only have seen
namespaced views). The data-dictionary/knowledge/recipes sites have the same shape:
multi-tenant workspaces silently show one arbitrary tenant. Reachable today from the
artifact panel and data-dictionary page (`frontend/src/store/dictionarySlice.ts:178,218`).

---

## Family F — Materialization dispatch + tracking (3 entry paths; 2 verbatim copies + 1 legacy orchestrator)

| Path | Entry | Dedup guard | ThreadJob | Pipeline runner |
|---|---|---|---|---|
| F1 | MCP `run_materialization` (`server.py:521`) | ACTIVE ThreadJob per **thread** (`:586–593`) | created after defer (`:625–632`), TODO race `tasks.py:373` | `materialize_workspace` |
| F2 | `materialization_retry_view` (`materialization_views.py:125`) | same filter, duplicated (`:171–182`, comment admits "Mirrors the MCP … guard") | duplicated (`:198–210`) | `materialize_workspace` |
| F3 | `POST /refresh/` (`api/views.py:325`) | `select_for_update` on PROVISIONING TenantSchema (`:354–361`) — a *different* dedup axis | none (orphan runs; see `materialization_views.py:60–62`) | **`refresh_tenant_schema` — a second orchestrator** |

F1 and F2 duplicate even the job-id extraction idiom
(`getattr(job, "id", job) if not isinstance(job, int) else job` — `server.py:615`,
`materialization_views.py:192`) and the failure path (best-effort
`cancel_job_by_id_async(job_id, abort=True)`). Two copies across two processes of a
sequence already known to be racy (`tasks.py:373` TODO) is how the next drift bug ships.

**Finding F (BROKEN-NOW / data-loss, verified-by-trace — independent re-verification of v1 run A's S1):**

`refresh_tenant_schema` and `run_pipeline` disagree about which schema to load:

1. Entry: `frontend/src/store/dictionarySlice.ts:197` → `POST /api/workspaces/<id>/refresh/`
   → `api/views.py:362` `SchemaManager().create_refresh_schema(tenant)` creates
   `{base}_r{8hex}` in PROVISIONING (`schema_manager.py:176`), then
   `refresh_tenant_schema.defer(...)` (`api/views.py:365`).
2. `tasks.py:126` `refresh_tenant_schema` → `tasks.py:177`
   `asyncio.to_thread(run_pipeline, membership, credential, pipeline_config)` —
   **the fresh schema is never passed**.
3. `materializer.py:183` `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`
   → `schema_manager.py:66` resolves the **base** name
   (`_sanitize_schema_name(tenant.external_id)`) and returns the existing ACTIVE base
   schema (`:68–78`). All data loads there.
4. Back in the task: `tasks.py:182–184` marks the **empty** `_r` schema ACTIVE;
   `tasks.py:189–198` flips every other ACTIVE schema for the tenant —
   **including the base schema that just received the data** — to TEARDOWN and defers
   `teardown_schema` with a 30-minute delay.
5. `tasks.py:610` `teardown_schema` drops the schema and marks runs STALE.

Net effect: refresh destroys the data it just loaded and leaves an empty ACTIVE schema.
Root cause in lens terms: two orchestrators (F3 vs `materialize_workspace`) sharing one
loader whose provisioning contract (`provision()` = base schema, always) only one of
them understands.

Related signalling inconsistency: tasks report failure two ways — raising (marks the
procrastinate job failed) vs. returning `{"error": ...}` dicts
(`tasks.py:135,144,…,229,246`), which procrastinate records as a **successful** job;
only consumers that re-read MaterializationRun rows see the failure.

---

## Family G — TTL "touch" (4 idioms, uneven coverage)

| Touch site | What it touches |
|---|---|
| `chat/views.py:151` → `workspace_service.py:74` `touch_workspace_schemas` | single: tenant schema; multi: **view schema + every constituent tenant schema** (docstring `:77–85` explains why constituents must be touched) |
| MCP `context.py:66` (`load_tenant_context`) / `context.py:125` (`load_workspace_context`) | single: tenant schema; multi: **view schema only — constituents NOT touched** |
| `artifacts/views.py:810–812` | the resolved tenant schema (first-tenant; see Family E) |
| `schema_manager.py:120–123` provision / `tasks.py:182–184` refresh | reset on (re)activation — the #228 incident fix, present at both sites |

Not touched at all: data-dictionary browsing (`api/views.py` — no touch despite
`models.py:49` "Call this on user-initiated actions").

**Finding G (LATENT / cost-correctness, touch map verified-by-trace, consequence strong-inference):**
for a multi-tenant workspace whose traffic flows only through MCP tools without a chat
message per TTL window (recipe runs via `recipes/services/runner.py`, resume-driven
turns), constituent TenantSchemas' `last_accessed_at` go stale while the view schema
stays fresh. `expire_inactive_schemas` (`tasks.py:537–544`) then TEARDOWNs a
constituent; `teardown_schema` → `_fail_dependent_view_schemas` (`tasks.py:654,666`)
flips the ACTIVE view schema to FAILED. The post-incident guard makes this *visible*
instead of silent, but the inconsistent touch semantics make it *reachable*: the same
"reset TTL on access" problem has two implementations and only one of them upholds the
documented invariant.

---

## Family H — Identifier composition & truncation (guard added at 1 of 4 sites)

The 2026-06-10 incident fix added a 63-byte guard for **view names** only
(`schema_manager.py:25–30` constants; `:300–349` bounded prefix, collision check,
hard-fail on oversize — all on final names before DDL; verified present and sound).

Unguarded siblings:

1. `schema_manager.py:625` `_sanitize_schema_name(tenant_id)` — no length cap. A
   sanitized `tenant.external_id` over 63 bytes would be silently truncated by
   PostgreSQL at `CREATE SCHEMA` while Django stores the full 255-char name
   (`models.py:33`), and two long external_ids sharing a 63-byte prefix collide into
   one physical schema — the exact mechanism of incident 1a, at the schema level.
2. `schema_manager.py:176` `create_refresh_schema` → `{sanitized}_r{8hex}` — adds 10
   chars to an already-unbounded name.
3. Read-only role names: `{schema_name}_ro`, derived **independently in two
   codebases** — `schema_manager.py:35` `readonly_role_name` and
   `mcp_server/context.py:31` `QueryContext.readonly_role`. A schema name of 61–63
   bytes yields a role name over the limit; `CREATE ROLE` truncates it, and two
   tenants sharing a 60-byte schema-name prefix would share one truncated `_ro` role —
   a cross-tenant read-access primitive.

**Finding H (LATENT / security-correctness, strong-inference):** no current provider
emits ids near the limit as far as I traced (Connect uses integer ids; OCS/CommCare
ids/domains are typically short), so this is latent — but the incident proved the class
is real, and the fix stopped at the site that bit. The `_ro` derivation being duplicated
across processes is additional drift surface regardless of length.

---

## Family I — Credential/token resolution (2 live resolvers + 1 dead consumer)

| | `aresolve_credential` (`users/services/credential_resolver.py:66`) | `get_user_oauth_tokens` (`agents/mcp_client.py:79`) |
|---|---|---|
| Provider match | prefix rules (`_social_token_qs:21–44`: `startswith("commcare_connect")`, `== "ocs"`, `startswith("commcare")` excl. connect) | exact `provider__in={"commcare","commcare_connect"}`; **no OCS** |
| Token refresh | yes, near-expiry (`:102–110`) | no — may hand out expired tokens |
| OCS team fail-closed guard | yes (`_oauth_team_mismatch:52`) | no |
| API-key support | yes | no |

**Finding I (DEBT / velocity, verified-by-trace):** the second resolver feeds a
pipeline with no consumer. `oauth_tokens` are gathered in `chat/views.py:162`, passed
to `build_agent_graph` — whose `oauth_tokens` parameter is **never used in the body**
(only `graph/base.py:485` signature and `:495` docstring reference it) — and placed in
LangGraph config (`chat/views.py:196`, `tasks.py:1154` "oauth_tokens through resume
config", a fix-chain commit). The only reader of that shape,
`mcp_server/auth.py:13` `extract_oauth_tokens`, has **zero callers**
(grep across the repo: definition only). The MCP client (`mcp_client.py:30–56`) forwards
nothing. Two resolvers with materially different semantics are maintained, one entirely
for a consumer that no longer exists — and fix effort (resume-config threading) has
been spent keeping the dead one plumbed.

---

## Family J — Loader/writer integrity idioms (13 writers, 3 regimes; retry & atomicity asymmetric)

**Write idioms (`mcp_server/services/materializer.py`):**

| Tables | Key/conflict | Transaction shape |
|---|---|---|
| CommCare `raw_cases`/`raw_forms` (`:1112,1179`); OCS ×4 (`:841–1052`) | natural PK + `ON CONFLICT DO UPDATE` | DROP/CREATE/INSERT in **one tx**, single outer commit (`_load_and_commit_source:714`) |
| Connect `raw_visits`/`raw_users` (`:1387,1499`) | natural key + ON CONFLICT | **commits per page** (`:1450,1490`; `:1621,1656`) |
| Connect ×5 (`completed_works`,`payments`,`invoices`,`assessments`,`completed_modules`, `:1573–1952`) | surrogate `GENERATED ALWAYS AS IDENTITY`, **plain INSERT, no ON CONFLICT** (`:1290–1336`) | DROP+CREATE committed immediately, then per-page commits (`:1710,1741` etc.) |

**Finding J1 (LATENT / data-loss window, verified-by-trace):** the docstring contract
at `materializer.py:689–691` — "Non-resumable path: the writer runs inside one
transaction; this function calls `conn.commit()` once at the end" — is **false for all
seven Connect writers**, which commit the DROP and every page unconditionally
regardless of `resumable`/`start_cursor`. Consequences: (a) a re-materialization that
fails on page 1 (Connect 5xx after retry exhaustion) has already committed the DROP —
the tenant's previously complete table is destroyed and not replaced, unlike OCS/
CommCare where the reload is atomic; (b) on cancel, the comment at
`materializer.py:311–314` ("this in-flight source rolled back") holds only for the
last partial page — earlier pages persist while `result["sources"]` records
`rows: 0`, and the partially-loaded table remains reachable by the raw `query` tool
even though the catalog hides it.

**Finding J2 (DEBT / correctness, verified-by-trace + git):** "which Connect sources
are resumable" is encoded in two places that contradict each other:
`_RESUMABLE_CONNECT_SOURCES` (`materializer.py:771`) lists 6 sources; YAML
(`pipelines/connect_sync.yml`) sets `resumable: false` on 5 of them. Git shows why:
#187 added cursors (5421344), f26c1a0 added real-id PKs + ON CONFLICT to fix
page-replay duplication, then 2587158 discovered the v2 export emits **no per-row id**,
reverted to IDENTITY PKs, dropped ON CONFLICT, and disabled resume *in YAML only* —
leaving the constant, the writers' `start_cursor` parameters, and the
`_max_id(page, "id")` cursor plumbing as live but dead-in-practice residue. The runtime
gate is `source_is_resumable = is_resumable_provider and source.resumable`
(`materializer.py:263`), so YAML currently wins — but `resumable` **defaults to True**
(`pipeline_registry.py:25,124`): any new Connect source that forgets the YAML flag
gets cursor-resume against a plain-INSERT writer, silently reintroducing the f26c1a0
duplication bug. Two sources of truth, default-unsafe.

**Finding J3 (LATENT / cost-correctness, verified by grep):** bounded retry exists only
in the Connect base loader (`loaders/connect_base.py:61` urllib3 `Retry`,
backoff, `respect_retry_after_header`); `commcare_base.py` and `ocs_base.py` call
`raise_for_status()` with no retry — one transient 500 fails the whole source (and for
those providers the whole-tx rollback discards everything loaded). The 2026-05-27
retry fix stopped at the provider that bit. Timeouts, by contrast, are consistent
across all three bases (verified).

---

## Family K — "Usable schema" predicate (one idiom, one divergent reader, one phantom state)

The predicate `state__in=[ACTIVE, MATERIALIZING]` is used consistently for
**TenantSchema** across ~10 sites (context.py:58, api/views.py:36,55, graph/base.py:213,
workspace_service.py:92,102, schema_manager.py:70, server.py:694, recipes/api/views.py:119,
backfill command). Good.

For **WorkspaceViewSchema** the implementations diverge:

- Builders use `PROVISIONING` as the "rebuilding" state
  (`schema_manager.py:286`, `workspace_service.py:26,71`).
- The query router accepts only `ACTIVE` (`context.py:115–118`).
- `get_schema_status` accepts `ACTIVE|MATERIALIZING` (`server.py:739–741`) — but
  **nothing ever sets MATERIALIZING on a WorkspaceViewSchema** (verified: all writes
  are PROVISIONING/ACTIVE/FAILED/TEARDOWN/EXPIRED in `schema_manager.py:286,428,436`,
  `tasks.py:588–607,666–690`, `workspace_service.py`); the MATERIALIZING arm is a
  copy-paste from the TenantSchema idiom and is dead.

**Finding K (LATENT / correctness, verified-by-trace):** during any multi-tenant
rebuild window (tenant added/removed, sibling rematerialization), the row is
PROVISIONING, so: the agent's `get_schema_status` reports **`not_provisioned`**
(`server.py:667–671` fallthrough — the FAILED check at `:746–752` doesn't match
PROVISIONING) and nudges the agent toward `run_materialization`, while the `query`
tool raises "No active view schema … Trigger a rebuild" (`context.py:119–123`) — both
messages wrong (a rebuild is already running), and the first invites a redundant
materialization dispatch. Three readers, three vocabularies for one state machine.

---

## Family L — Prompt ↔ tool-contract drift (recurring class, live instance)

`graph/base.py:222–223` (single-tenant context):
`Call run_materialization with pipeline="{pipeline_name}" …` — but the tool takes no
`pipeline` parameter (`server.py:521–527`: workspace_id/user_id/thread_id/tool_call_id,
all hidden from the LLM via `_llm_tool_schemas`, so the LLM-facing schema has **zero**
properties). The multi-tenant sibling builder says simply "Call `run_materialization`"
(`graph/base.py:338`) — one prompt was updated when the parameter died, its sibling
wasn't. Same class as #190/93504d5 (prompt vs SQL allow-list), which is currently
aligned (verified: both say SELECT-only).

**Finding L (DEBT / correctness, drift verified; runtime consequence hypothesis):**
depending on how FastMCP handles unexpected arguments, the instructed
`pipeline="commcare_sync"` argument either errors or is silently dropped; either way
the instruction is wrong and this is the third occurrence of the prompt/contract-drift
class. The deeper inconsistency: prompts duplicating tool-shape knowledge in prose, per
prompt builder, with no single source.

---

## Family M — Small consistency residue (COSMETIC/DEBT, all verified)

- **Share surfaces, two vocabularies:** Thread uses `is_shared`+`share_token` with a
  save()-enforced invariant (`chat/models.py:42–47`); Recipe/RecipeRun use
  `is_public`+`share_token` with the same pattern under a different flag name
  (`recipes/models.py:117–120,388–391`). Share-creation UI was removed 2026-06-04 but
  `thread_share_view` PATCH (`thread_views.py:163`) and both public endpoints remain
  live — drift between UI affordance and API surface.
- **Three auth-guard implementations** (`users/decorators.py`: `async_login_required`
  sets `request._authenticated_user`; `login_required_json`; `LoginRequiredJsonMixin`) —
  currently behaviorally aligned (401 JSON), acceptable sync/async/CBV triplet, but the
  `_authenticated_user` side-channel is a convention only the async family has.
- **touch/atouch duplicated per model** (`workspaces/models.py:48–56,247–255`) plus a
  third queryset-`aupdate` idiom (`workspace_service.py:102`).
- **Stale Celery docstring**: `api/views.py:319` "dispatches a Celery task" — Celery
  was removed 2026-05-01; comment/code mismatch.
- **Error-payload styles**: `_ACCESS_DENIED` dict vs ad-hoc `{"error": ...}` strings vs
  DRF `Response` — N styles, no envelope on the Django side (MCP side does have a
  uniform envelope, `mcp_server/envelope.py`, used by all tools I read).

---

## What's actually fine (verified healthy)

- `cancel_thread_job` is a genuine consolidation: both Django cancel endpoints funnel
  through it, and the DB-flip-before-abort ordering is implemented and documented
  consistently (`jobs_cancel.py:3–6,28–52`).
- `aresolve_credential` is the **only** credential resolver on the materialization
  path (the sync variant is gone); the SynchronousOnlyOperation class from project
  memory is structurally addressed at `tasks.py:158–160` with an explanatory comment
  that matches the code.
- The TenantSchema `ACTIVE|MATERIALIZING` predicate is used uniformly (~10 sites).
- The post-#227 view-name builder does length + collision checks on final names
  before any DDL (`schema_manager.py:296–352`) — the fixed site is fixed well.
- Post-#228 TTL resets on activation exist at **both** provisioning sites
  (`schema_manager.py:115–123`, `tasks.py:179–184`) — the fix was applied to siblings.
- `_aggregate_materialization_state` is a real single source of truth for resume
  status (only caller-pair in `tasks.py`), the consolidation outcome of the May fix chain.
- System prompt and SQL validator currently agree (SELECT-only).
- All three loader bases apply HTTP timeouts via shared constants.
- `workspace_resolver` is genuinely shared by every Django API surface for membership
  *existence* — the inconsistency is in what callers do with the role, not in the lookup.

---

## Coverage log

**Deep-read (line-level):** `apps/workspaces/workspace_resolver.py`, `permissions.py`,
`models.py`, `services/workspace_service.py`, `services/schema_manager.py` (lines
20–360, 600–631), `tasks.py` (lines 64–360, 460–700, 770–820 region, 928–1020),
`api/materialization_views.py`, `api/jobs_cancel.py`, `api/jobs_views.py` (1–95),
`api/views.py` (32–63, 230–260, 319–400, 470–510), `api/workspace_views.py` (236–300 +
role-check greps); `apps/chat/helpers.py`, `chat/models.py` (share fields),
`thread_views.py` (160–200); `apps/users/services/credential_resolver.py`,
`users/decorators.py`; `apps/agents/mcp_client.py`, `agents/graph/base.py` (60–80,
180–420, 480–540); `apps/artifacts/views.py` (673–845 region); `mcp_server/context.py`,
`mcp_server/auth.py`, `mcp_server/server.py` (400–760), `mcp_server/services/
materializer.py` (80–330, 380–520, 560–860, 890–945, 1112–1140, 1265–1345, 1573–1760 +
commit-site grep), `pipelines/*.yml`, `mcp_server/pipeline_registry.py` (resumable
default), loader base files (retry/timeout sections). Git archaeology: f26c1a0,
2587158, 5421344, connect_sync.yml history.

**Skimmed (outline/grep only):** `mcp_server/services/metadata.py` (function outline +
key fields), `knowledge/api/views.py`, `recipes/api/views.py` + `models.py` +
`services/runner.py` (greps), `apps/chat/views.py` (greps for oauth/touch/resolver),
`frontend/src/store/dictionarySlice.ts` (endpoint greps), `users/services/
token_refresh.py` (via resolver), `mcp_server/services/sql_validator.py` +
`prompts/base_system.py` (allow-list alignment greps), `transformations/views.py`
(role greps), management commands (grep hits only).

**Not examined (honest gaps for the gap loop):**
- Frontend beyond grep: `MaterializationProgressBanner`, `useWorkspaceJobs`,
  `useWorkspaceThreadSync`, hand-written TS types vs API shapes (status-string unions
  could hide a Family-C divergence I did not check).
- `apps/transformations/` services (executor, lineage, dbt_project, commcare_staging) —
  potential duplicate "catalog" and "run-state" implementations unreviewed.
- `users/services/merge.py`, `signals.py`, `tenant_resolution.py`, adapters, OAuth
  provider modules — access-resolution siblings at the *account* layer not compared.
- `mcp_server/services/query.py` and `sql_validator.py` internals; `envelope.py`
  envelope-usage audit across all 11 tools (I verified the ~6 I read).
- `chat/stream.py`, `message_converter.py`, both checkpointer modules — possible
  duplicate message-shape conversions.
- `artifacts/services/export.py`; public pages/widget.js beyond noting they're live.
- `expire_stale_thread_jobs` janitor + `_procrastinate_job_status` mapping
  (`tasks.py:693–840`) — read only in part; single-implementation so out of lens, but
  its status mapping was not verified against procrastinate semantics.
- Tests (`tests/`, 27k LOC) — not used as evidence anywhere above.
- OCS/CommCare loader pagination internals beyond the base classes (per-loader
  `load_pages` semantics not compared item-by-item).
