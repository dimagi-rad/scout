# Vertical review: Data dictionary / table metadata / catalog

*Reviewer: vertical:catalog-dictionary. Date: 2026-06-12. Repo HEAD: 35e4230 (main).*
*Mandate: own every implementation of "what tables exist / what is their status" across
MCP, Django API, prompt assembly, and frontend; enumerate them; find where they disagree.*

---

## 1. Census: implementations of "what tables exist"

Seven distinct sources of truth, each with a different algorithm:

| # | Implementation | Algorithm | Consumers |
|---|---|---|---|
| T1 | `mcp_server/services/metadata.py:pipeline_list_tables` (29–112) | last COMPLETED/PARTIAL `MaterializationRun.result["sources"]` where source `state=="completed"`, intersected with `information_schema.tables`, **plus** pipeline `dbt_models` listed optimistically | MCP `list_tables` (single-tenant), `pipeline_get_metadata`, `DataDictionaryView`, `TableDetailView`, prompt `_fetch_schema_context` (no-transformation case) |
| T2 | `metadata.py:workspace_list_tables` (161–185) | `information_schema.tables WHERE table_type='VIEW'` in the `ws_*` schema | MCP `list_tables` (multi-tenant), MCP `get_schema_status` (multi-tenant), prompt `_fetch_multi_tenant_schema_context` |
| T3 | `metadata.py:transformation_aware_list_tables` (269–325) | T1 minus replaced raw tables, plus terminal `TransformationAsset` rows | **only** prompt assembly `apps/agents/graph/base.py:249` — *not* the MCP `list_tables` tool |
| T4 | MCP `get_schema_status` single-tenant branch, `mcp_server/server.py:718–724` | `last_run.result["tables"]` or legacy `result["table"]`+`rows_loaded` | the agent (MCP tool). **Dead keys** — the materializer only ever writes `result["sources"]` (materializer.py:366–368, 398, 465–466) |
| T5 | `Workspace.data_dictionary` JSONField (models.py:132) | legacy snapshot | `TableDetailView._get_table_data` fallback (views.py:425–427), `learning_tool.py:168` validation. **No writer exists** — only `purge_synced_data` clears it |
| T6 | Django-side `_get_all_columns`/`_get_table_columns` (`apps/workspaces/api/views.py:73–137`) | sync psycopg `information_schema.columns` query | data-dictionary UI column truth |
| T7 | The live `search_path` reality the `query` tool hits (`mcp_server/services/query.py` + `context.py`) | PostgreSQL itself | every agent query |

## 2. Census: implementations of "what is the data's status"

| # | Implementation | Derivation |
|---|---|---|
| S1 | `RefreshStatusView` (views.py:373–402) | latest `TenantSchema` of first tenant by `created_at` |
| S2 | MCP `get_schema_status` (server.py:651–798) | single: TenantSchema + last run; multi: `WorkspaceViewSchema` incl. FAILED+`last_error` (PR #229) |
| S3 | Prompt `_fetch_schema_context` (base.py:205–296) | `ts.state` (incl. a **dead** MATERIALIZING branch), then T3/T1 |
| S4 | Prompt `_fetch_multi_tenant_schema_context` (base.py:306–393) | active `MaterializationRun` rows + `vs.state`; FAILED collapses into "no data loaded" |
| S5 | `_derive_schema_status`/`_schema_status_for_workspaces` (workspace_views.py:30–106) | bulk per-tenant schema states + view-schema state, for workspace list/detail cards |
| S6 | `_aggregate_materialization_state` (tasks.py:928–1016) | per-run/source rollup for the resume prompt |
| S7 | `_schema_unavailable_response` (views.py:40–70) | 503 gate for dictionary endpoints, first tenant only |
| S8 | Frontend `dictionaryStatus` (`dictionarySlice.ts:104`) | 503 → `not_materialized`, else error/loaded |

The mandate's hypothesis ("five table-catalog implementations that can disagree") is
**under-counted**: there are 7 table-list implementations and 8 status implementations.

---

## 3. Findings

### F1 — Legacy `/refresh/` destroys the tenant's data (refresh loads into the base schema, then tears the base schema down)

- **Status: BROKEN-NOW · Impact: data-loss · Confidence: verified-by-trace · Complexity: accidental**
- **Reachable via**: the always-visible refresh button on the Data Dictionary page
  (`DataDictionaryPage.tsx:95–106`, `data-testid="refresh-schema-btn"`) →
  `dictionarySlice.ts:197` `POST /api/workspaces/<id>/refresh/` → wired at
  `apps/workspaces/api/urls.py:22`.

Chain (every hop quoted):

1. `RefreshSchemaView.post` (views.py:325) → `SchemaManager().create_refresh_schema(tenant)`
   (views.py:362) creates a **new uniquely-named** schema record:
   `schema_name = f"{self._sanitize_schema_name(tenant.external_id)}_r{uuid.uuid4().hex[:8]}"`
   (schema_manager.py:176), then `refresh_tenant_schema.defer(...)` (views.py:365).
2. `refresh_tenant_schema` (tasks.py:126) creates the physical `_r` schema
   (tasks.py:150) and then calls `run_pipeline(membership, credential, pipeline_config)`
   (tasks.py:173) — **passing no schema**.
3. `run_pipeline` picks its own target: `tenant_schema = SchemaManager().provision(tenant_membership.tenant)`
   (materializer.py:183). `provision()` resolves the **base** sanitized name
   (schema_manager.py:66) and returns the existing ACTIVE base schema
   (schema_manager.py:68–78). All data is loaded into the **base** schema; the
   `MaterializationRun` rows attach to the base `TenantSchema` (materializer.py:186–191).
4. Back in the task: `new_schema.state = SchemaState.ACTIVE` (tasks.py:182) — the **empty**
   `_r` schema is activated.
5. Then every other ACTIVE schema for the tenant — i.e. the base schema that just received
   the fresh data — is flipped to TEARDOWN and a 30-minute-delayed `teardown_schema` is
   deferred (tasks.py:188–197), which **drops the schema and the data**.

Aftermath: the tenant's only ACTIVE schema is the empty `_r` schema; T1 finds no
`MaterializationRun` for it → `list_tables` returns `[]` with the note "Run
run_materialization" (server.py:163–167); the dictionary returns `{"tables": {}}`
(views.py:279); the agent prompt says "Data is loaded but no tables are available yet"
(base.py:254). `RefreshStatusView` reports `state: "active"` for the empty schema
(views.py:391–402) — every status surface confirms a healthy, empty world.
This replicates v1 run A's S1; this trace confirms it end-to-end including the UI trigger.

### F2 — MCP `get_metadata` reports 0 tables for every multi-tenant workspace, while `list_tables` reports the views

- **Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**
- **Reachable via**: `get_metadata` is in `MCP_TOOL_NAMES` (base.py:65–76) and bound to the LLM.

`list_tables` special-cases view schemas (server.py:127–137 → `workspace_list_tables`).
`get_metadata` does not: it looks up `TenantSchema.objects.filter(schema_name=ctx.schema_name)`
(server.py:248); for a multi-tenant workspace `ctx.schema_name` is the `ws_<hex>` view
schema (context.py:113–139), which never matches a `TenantSchema`, so it returns
`{"table_count": 0, "tables": {}, "relationships": []}` (server.py:249–255). The agent can
receive "here are 8 tables" from one tool and "0 tables" from another in the same turn —
exactly the contradictory-schema-response class behind incident #190's panic loops (the
circuit breaker won't trigger here because both responses are `success`).

### F3 — Prompt tells the agent "No data has been loaded yet" for a FAILED multi-tenant view schema, undoing PR #229's fix

- **Status: BROKEN-NOW (whenever a view-schema build fails) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**
- **Reachable via**: every chat turn in a multi-tenant workspace whose `WorkspaceViewSchema.state == FAILED`.

`get_schema_status` was changed post-incident to surface FAILED with `last_error`, with an
in-code comment that the agent "must NOT mistake [it] for 'just run materialization'"
(server.py:744–764). But the system prompt — which the agent reads before any tool call —
collapses FAILED into the same branch as never-built:
`if vs is None or vs.state != SchemaState.ACTIVE:` → "No data has been loaded yet. Call
`run_materialization` to start loading…" (base.py:334–343). Two of the four status
implementations that the agent sees disagree about the same row, and the prompt's claim
("no data loaded") is factually false in the FAILED case (per-tenant data *did* load).

### F4 — `SchemaState.MATERIALIZING` is written by no production code; the prompt's "materialization in progress" guard for single-tenant workspaces is dead, enabling double-dispatch

- **Status: BROKEN-NOW · Impact: correctness + cost-perf · Confidence: verified-by-trace (dead state), strong-inference (double-dispatch consequence) · Complexity: accidental**

Grep over `apps/` + `mcp_server/` finds **no assignment** of `MATERIALIZING` outside test
fixtures (`tests/test_workspace_service.py:133`, `tests/agents/test_schema_context.py:46,300`);
`provision()` goes PROVISIONING→ACTIVE directly (schema_manager.py:120–122). Yet eight
readers filter on or branch on it, including:

- `_fetch_schema_context`'s `if ts.state == SchemaState.MATERIALIZING:` "already in
  progress … do NOT trigger another one" (base.py:230–237) — **unreachable in production**;
  the tests that cover it hand-set the state, masking the gap.
- The multi-tenant twin checks live `MaterializationRun.ACTIVE_STATES` instead
  (base.py:320–325) and works.

Consequence: during an in-flight **single-tenant** materialization the schema is ACTIVE
with no completed run, so a second thread's prompt says "Data is loaded but no tables are
available yet" (base.py:254) and MCP `list_tables` appends "No completed materialization
run found. Run run_materialization to load data." (server.py:163–166). The only dedupe
guard is per-thread (`server.py:586–590`, with a comment conceding "this lets two threads
in the same workspace dispatch parallel materializations… the materializer has no advisory
lock per tenant_schema"). The asymmetry between S3 and S4 is the mechanism.

### F5 — `get_schema_status` single-tenant `tables` is keyed to an extinct result shape: always `[]`

- **Status: BROKEN-NOW · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental (rename residue)**
- **Reachable via**: MCP tool, bound to LLM.

server.py:718–724 reads `last_run.result["tables"]` else `result["table"]`+`rows_loaded`
(and emits the pre-rename key `row_count`). The materializer writes only
`{"sources": ...}` (materializer.py:366–368, 398, 424, 440, 465–466, 503, 623); grep finds
no other writer. So the tool answers `exists: true, state: active, tables: []` after every
successful run — disagreeing with `list_tables` in the same conversation. The docstring's
"table list" promise is a claim the code no longer honors.

### F6 — Three different table catalogs when transformation assets exist: prompt ≠ MCP `list_tables` ≠ dictionary UI

- **Status: BROKEN-NOW (for tenants with TransformationAssets, e.g. all CommCare tenants via `upsert_system_assets`, materializer.py:217–229) · Impact: correctness · Confidence: verified-by-trace (call sites) · Complexity: accidental**

`transformation_aware_list_tables` (replaces raw tables with terminal assets) is called
**only** from prompt assembly (base.py:249); grep confirms no other production caller.
The MCP `list_tables` tool uses plain `pipeline_list_tables` (server.py:161) → raw tables
only. The dictionary UI also uses `pipeline_list_tables` and additionally filters
`stg_` (views.py:273–277). So with transformations active: the prompt advertises terminal
model names, `list_tables` returns raw names, and the UI shows raw-minus-staging. The
agent is told up-front about tables that its own catalog tool then denies — again the
#190 contradiction class.

### F7 — `TableKnowledge` annotations are keyed to physical schema names that churn and that the agent can't see

- **Status: LATENT (multi-tenant: BROKEN-NOW) · Impact: correctness · Confidence: verified-by-trace (keying), strong-inference (orphaning) · Complexity: accidental**

The dictionary writes annotations under `qualified_name = f"{schema_name}.{table_name}"`
(views.py:289–290, 524–527). Consequences:

- The legacy refresh path renames the schema (`…_r{uuid}`), orphaning every annotation
  (lookup at views.py:219 is exact-string).
- In multi-tenant workspaces, the agent queries `ws_*` views named `{tenant}__{table}`
  (base.py:299–303), but `KnowledgeRetriever._format_table_knowledge` injects headings
  like `t_12345.cases` into the prompt (retriever.py:78) — names that don't exist in the
  schema the agent queries.
- `Learning.applies_to_tables` and `related_tables` are bare-string matched, same family.

### F8 — `TenantMetadata` is per-membership but read with three different scopes; annotations appear and disappear per user

- **Status: DEBT · Impact: correctness · Confidence: strong-inference · Complexity: accidental**

The materializer writes metadata to the **triggering user's** membership only
(`TenantMetadata.objects.update_or_create(tenant_membership=tenant_membership, ...)`,
materializer.py:539–541; model is OneToOne to TenantMembership, workspaces/models.py:266).
Readers:

- prompt: filtered to `tenant_membership__user=user` (base.py:263–265) → users who never
  ran materialization get **no** JSONB column annotations in their prompt;
- MCP `describe_table`/`get_metadata`: `filter(tenant_membership__tenant_id=…).afirst()`
  (server.py:210–212, 270–272) → arbitrary membership;
- dictionary: `filter(tenant_membership__tenant=tenant).first()` (views.py:194–196) →
  arbitrary, possibly a stale row from another user.

Same tenant, same table, three different answers depending on surface and user.

### F9 — `pipeline_list_tables` is fail-closed for sources but fail-open for dbt models; docstring claims otherwise

- **Status: LATENT (all three current pipeline YAMLs declare no `transforms.models`, so the branch is moot today) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

On `information_schema` failure `_live_tables_in_schema` returns `set()` with a docstring
saying "a transient DB error surfaces as an empty list rather than phantom rows"
(metadata.py:119–121). True for sources (`if physical_name not in live_table_names:
continue`, metadata.py:83–84) — but dbt models are then listed *optimistically*
(`if live_table_names and model_name not in live_table_names: continue`, metadata.py:97–99).
If any pipeline regains a models list, a transient DB blip yields a catalog of only
maybe-nonexistent dbt tables → NOT_FOUND streak → escalation breaker. Comment/code
mismatch is itself a finding per the evidence standards.

### F10 — `Workspace.data_dictionary` is a writer-less vestige; its two remaining readers are dead or no-ops

- **Status: DEBT (dead code) · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

Grep finds no writer (only `purge_synced_data` nulls it). Readers: `TableDetailView`
fallback (views.py:425–427 — can only ever return None for new workspaces) and
`learning_tool.py:168–179`, where `known_tables` is always empty so the
"validate tables exist" check is permanently skipped. The model comment "Legacy fields
retained" (models.py:131) is honest; the readers pretending it's populated are not.

### F11 — Data Dictionary UI in a multi-tenant workspace shows only the first tenant, gated on the first tenant's schema

- **Status: BROKEN-NOW (multi-tenant workspaces) · Impact: correctness · Confidence: verified-by-trace · Complexity: accidental**

`DataDictionaryView.get` uses `workspace.tenant` — the documented "single-tenant
compatibility: returns the first associated tenant" shim (models.py:143–146) — for both
the 503 gate (views.py:241) and the table list (views.py:245). `TableDetailView` and
`RefreshStatusView` (views.py:387–391) do the same. A user in a 3-tenant workspace sees
one tenant's raw tables under `t_*` names while their chat agent sees `tenant__table`
views in `ws_*`; tenants 2..n are invisible; if tenant 1's schema expired but the view
schema is ACTIVE, the dictionary 503s while chat works. Annotation writes (F7) then key
to names the agent never uses. Status census S1/S7 vs S2/S4 disagree by construction.

### F12 — Pipeline-config fallback chain duplicated 3×; Django imports `mcp_server` internals

- **Status: DEBT · Impact: velocity · Confidence: verified-by-trace · Complexity: accidental**

The "last run's pipeline → provider pipeline → commcare_sync" cascade exists at
views.py:265–271, views.py:445–451, and server.py:78–101 (plus a fourth variant at
base.py:217–218 / 739–741 that skips the last-run step — so the prompt can describe a
different pipeline than the tools use for a tenant whose provider mapping changed).
`apps/workspaces/api/views.py:26–27` and `apps/agents/graph/base.py:47–54` import
`mcp_server.*` directly into the Django process — the "standalone MCP server" boundary is
one-directional fiction; any metadata.py change ships in three processes.

### F13 — Knowledge export/import covers only `KnowledgeEntry`; table annotations and learnings are not portable

- **Status: DEBT · Impact: velocity · Confidence: verified-by-trace**

`KnowledgeExportView`/`KnowledgeImportView` (knowledge/api/views.py:227–318) serialize
`KnowledgeEntry` only. `TableKnowledge` (the data-dictionary annotations) and
`AgentLearning` have no export path, so the curated catalog layer cannot be backed up or
moved between workspaces — relevant because F1/F7 are exactly the scenarios that destroy it.

### F14 — Cosmetic drift cluster

- **Status: COSMETIC · Impact: velocity · Confidence: verified-by-trace**

- "dispatches a Celery task" in `RefreshSchemaView` docstring (views.py:319) and
  `create_refresh_schema` (schema_manager.py:174) — Celery was replaced 2026-05-01.
- Prompt's "Canonical Metrics (CRITICAL)" section (base_system.py:81–94) references a
  concept whose model (`CanonicalMetric`) was replaced by generic `KnowledgeEntry`
  (knowledge/models.py:84–91); the retriever emits no canonical-metric structure.
- Double heading: `_build_system_prompt` wraps retriever output in `## Knowledge Base`
  (base.py:733) and `_format_knowledge_entries` emits its own `## Knowledge Base`
  (retriever.py:58).
- `DataDictionaryView` docstring says "GET /api/data-dictionary/" (actual route is
  workspace-scoped) and "Sources table metadata from the latest completed
  MaterializationRun" (also accepts PARTIAL).
- Inline imports inside `_fetch_schema_context` (base.py:244, 261) violate the project's
  own import policy.

---

## 4. Disagreement matrix (the headline deliverable)

For one workspace, "what tables exist" by surface:

| Scenario | Prompt (S3/S4) | MCP `list_tables` | MCP `get_metadata` | MCP `get_schema_status` | Dictionary UI |
|---|---|---|---|---|---|
| Single-tenant, no transforms | T1 | T1 | T1 | **`[]` (F5)** | T1 minus `stg_` + Django columns |
| Single-tenant, transforms | **T3 (terminal assets)** | **T1 (raw)** | T1 (raw) | `[]` | T1 minus `stg_` |
| Single-tenant, mid-materialization | "loaded, no tables yet" (F4) | `[]` + "run materialization" | `{}` | `exists:false` or stale | 503/empty |
| Multi-tenant, ACTIVE views | T2 (names only, no columns/counts) | T2 | **0 tables (F2)** | T2 | **first tenant's T1 (F11)** |
| Multi-tenant, FAILED view build | **"no data loaded, run materialization" (F3)** | error (no ACTIVE vs → context fails) | error | `state:failed` + `last_error` | first tenant's T1 |
| Post-`/refresh/` | "loaded, no tables yet" | `[]` | `{}` | `[]` | empty — **data destroyed (F1)** |

## 5. Percentage functional, per capability

- **Single-tenant dictionary UI + annotations (the demo path)**: ~85%. Works end-to-end;
  weakened by F8 (annotation visibility varies by user) and the `get_schema_status`
  tables hole (F5).
- **MCP metadata tools, single-tenant**: ~80% (`list_tables`/`describe_table`/
  `get_metadata` coherent; `get_schema_status` tables broken; in-flight detection absent F4).
- **MCP metadata tools, multi-tenant**: ~55% (`get_metadata` flatly wrong F2; status
  asymmetries F3).
- **Dictionary UI, multi-tenant**: ~30% (wrong tenant scope, wrong names, wrong gate — F11).
- **Catalog with transformations active**: ~50% (three surfaces, three answers — F6).
- **Legacy `/refresh/`**: 0%, net-negative (destroys data — F1).
- **Prompt schema-context assembly**: ~70% (correct and well-budgeted on the happy path;
  F3/F4/F6/F7/F8 each corrupt it in a specific state).
- **Catalog reconciliation (#185 fix)**: healthy for sources; the dbt-model branch is the
  remaining fail-open (F9, currently moot).

## 6. What's fine

- **Phantom-row reconciliation (T1 core)**: sources are correctly intersected with
  `information_schema` and gated on per-source `completed` state, including the #187
  `in_progress` resume case (metadata.py:38–94).
- **`materialized_row_count` honesty chain**: the rename, `row_count_verified: false`,
  the prompt-table footnote (base.py:162–166), and the BASE_SYSTEM_PROMPT rules
  (base_system.py:116–157) are mutually consistent — a genuinely well-executed contract.
- **Prompt ↔ SQL-validator alignment post-93504d5**: "no information_schema, unqualified
  pg_catalog reachable" (base_system.py:167) matches `_validate_table_access`'s
  schema-qualifier-only enforcement (sql_validator.py:269–288).
- **Escalation breaker ↔ prompt**: "three NOT_FOUND in a row → escalate" matches
  `ESCALATION_TRIGGER_COUNT = 3` (base.py:88, base_system.py:156–157).
- **`_live_tables_in_schema` connection-params fix**: the docstring documents a real
  past bug and the code matches (metadata.py:122–143).
- **Provision TTL fix (#228)**: resurrect path now resets `last_accessed_at`
  (schema_manager.py:114–122) — the incident fix is present and coherent.
- **System-prompt cache**: keyed on workspace+user+prompt-hash with short TTL and
  eviction (base.py:127–142, 774–783); staleness window is bounded and intentional.

## 7. Essential vs accidental

Essential complexity here is small: "list the tables the agent may query, with their
freshness." Nearly everything found is accidental: the single→multi-tenant migration
left compat shims (`workspace.tenant`) live in the dictionary; the catalog grew by
accretion (7 list implementations) instead of one resolver with per-surface renderers;
the refresh path predates the materializer owning provisioning and was never reconciled;
status grew one implementation per consumer (8 total). The one structural fix that
collapses the most findings: a single `catalog_for_workspace(workspace) -> (tables,
status)` service used by MCP tools, prompt assembly, and the dictionary API alike —
F2, F3, F4, F5, F6, F11 are all "two renderers diverged" instances.

## 8. Coverage log

**Deep-read** (line-by-line): `apps/workspaces/api/views.py`; `apps/workspaces/models.py`;
`apps/knowledge/models.py`; `apps/knowledge/api/views.py`; `apps/knowledge/services/retriever.py`;
`mcp_server/services/metadata.py`; `mcp_server/server.py`; `mcp_server/context.py`;
`mcp_server/pipeline_registry.py`; `mcp_server/services/sql_validator.py`;
`apps/agents/graph/base.py`; `apps/agents/prompts/base_system.py`;
`apps/workspaces/services/schema_manager.py` (lines 50–185);
`apps/workspaces/tasks.py` (lines 120–210, 928–1020 + function outline);
`mcp_server/services/materializer.py` (lines 90–250, 1055–1080 + targeted greps);
`apps/agents/tools/learning_tool.py` (130–210);
`frontend/src/store/dictionarySlice.ts`; `frontend/src/pages/DataDictionaryPage/DataDictionaryPage.tsx`;
`apps/workspaces/api/workspace_views.py` (lines 40–130); `pipelines/commcare_sync.yml`.

**Skimmed**: `apps/workspaces/api/urls.py` (grep), `pipelines/connect_sync.yml` /
`ocs_sync.yml` (models-section grep only), `mcp_server/services/query.py` (signatures via
imports), git history of views.py / MATERIALIZING.

**Not examined**: `mcp_server/loaders/*` (19 files); `materializer.py` writers/cursor
logic beyond the quoted ranges; `apps/transformations/*` (lineage internals,
`upsert_system_assets`, dbt execution — I assert CommCare tenants get TransformationAssets
from materializer.py:217–229 but did not trace `upsert_system_assets` itself);
`frontend/src/pages/DataDictionaryPage/SchemaTree.tsx` and `TableDetail.tsx`;
`WorkspaceDetailPage.tsx` data-sources tab; `frontend MaterializationProgressBanner` /
jobs context (S8 beyond the slice); `apps/knowledge/serializers.py` and `utils.py`;
`mcp_server/envelope.py`; `tests/` (except targeted greps); `apps/workspaces/tasks.py`
lines 210–928 and 1020–end (materialize_workspace body, janitors, resume body);
admin.py registrations; management commands beyond grep.
