# Tenant and Workspace Transformation Architecture (v3)

## Summary

Design a layered data architecture that moves Scout from raw JSON-heavy source tables to a managed analytical model using dbt as the transformation engine. Three transformation scopes:

- **System** — provider standardization (staging layer), auto-generated from metadata  
- **Tenant** — shared business rules, exclusions, feature generation, enrichment  
- **Workspace** — workspace-specific joins, rollups, cross-tenant compositions, derived marts

dbt manages execution order via its DAG (built from `ref()` dependencies) within each scope. Execution is orchestrated in three sequential stages: system → tenant → workspace. Each `.sql` file is one dbt model producing one table or view. All transformations run automatically on data refresh.

CommCare is the first fully specified provider; Connect fits the same contract but does not need equal detail in this first spec.

## Key Changes

### 1\. Introduce a provider contract with a standardized system layer

Define a provider pipeline contract with distinct responsibilities:

- **Raw ingestion**: fetch and preserve source payloads with provenance (`raw_*` tables)  
- **Metadata discovery**: fetch schema-defining metadata needed to interpret the source (app definitions, case types, form structures including repeat groups)  
- **Standardization**: generate stable, typed, relational analytical tables from raw \+ metadata (`stg_*` tables)

#### CommCare staging: fully relationalized

Staging is intentionally opinionated. Rather than a single `stg_cases` table with a JSONB properties column, the system generates **one table per case type** and **one table per form xmlns**, with repeat groups as child tables. The SQL for these models is dynamically generated from metadata discovered in the DISCOVER phase.

**Naming convention:**

| Entity | Pattern | Example |
| :---- | :---- | :---- |
| Cases by type | `stg_case_{case_type}` | `stg_case_patient`, `stg_case_household` |
| Forms by xmlns | `stg_form_{form_name}` | `stg_form_registration`, `stg_form_followup_visit` |
| Repeat groups | `stg_form_{form_name}__repeat_{group}` | `stg_form_followup_visit__repeat_medications` |

- Form names are slugified from the app JSON `form.name`. If two forms across apps share a name, disambiguate with `_{app_slug}` suffix.  
- Repeat group child tables have a FK back to the parent form row via `form_id` \+ a repeat index.  
- Case property names and form question names become typed columns. Properties not present in the current app definition (historical/deleted) remain in the raw JSONB — users who need them can extract them via tenant-level models.

#### Metadata source

The existing DISCOVER phase fetches application JSON from CommCare HQ (via the Application API), which provides case types, form structures (including repeat groups), question labels, and case-type mappings. This is sufficient for v1. The app definition is the sole metadata source — no raw data introspection.

#### Dynamic SQL generation

System staging dbt models are dynamically generated, not static `.sql` files:

1. DISCOVER phase fetches app JSON, stores in `TenantMetadata`  
2. Materializer reads metadata, generates `TransformationAsset` records (`scope=system`) with SQL — one per case type, form, and repeat group  
3. Before dbt runs, materializer writes all relevant assets to a **temp directory** as `.sql` files  
4. dbt runs against that temp directory  
5. Temp directory is cleaned up

The database (`TransformationAsset`) is the source of truth. Disk is ephemeral — just the format dbt needs to execute.

Connect should be named in the spec as a supported future implementation of the same contract, with the note that its exact sourcing strategy vs CommCare can be decided later.

### 2\. Use dbt as the transformation engine

All transformation layers — system staging, tenant, and workspace — are dbt models:

- **One model per `.sql` file**, each producing one table or view  
- **`ref()` for dependencies within a scope**: models reference upstream models in the same scope via `{{ ref('model_name') }}`  
- **Direct table names across scopes**: workspace models reference tenant/system tables by their materialized table name (not `ref()` or `source()`), since the three-stage pipeline guarantees ordering  
- **DAG-based execution within a scope**: dbt resolves the dependency graph for models within the same stage  
- **Three-stage sequential execution**: system → tenant → workspace. Each stage is a separate dbt run.  
- **Additive only**: custom models create new relations; they may not overwrite raw or system-owned staging tables

#### dbt layering convention

| Layer | Prefix/convention | What it does | Visibility |
| :---- | :---- | :---- | :---- |
| Raw | `raw_*` | Exact source payloads from providers | Audit trail; agent deprioritizes |
| Staging | `stg_*` | System standardization — type casting, JSON flattening, full relationalization | Agent \+ all users (clean base) |
| Tenant | User-named | Shared business rules, exclusions, feature generation, enrichment | Agent \+ all workspaces on that tenant |
| Workspace | User-named | Workspace-specific joins, rollups, cross-tenant compositions, derived marts | Agent \+ that workspace only |

### 3\. Add data quality tests

dbt's YAML-based testing framework provides data quality validation at each layer:

- **Schema tests** defined in `.yml` files alongside models: `unique`, `not_null`, `accepted_values`, `relationships`  
- Tests run after models are built during the transform phase  
- Test failures are logged and surfaced in run status but do not block downstream models  
- Users can author custom tests for their tenant and workspace models

Example schema test (in a `.yml` file alongside models):

```
models:
  - name: stg_case_patient
    columns:
      - name: case_id
        tests:
          - unique
          - not_null
      - name: case_type
        tests:
          - not_null
```

### 4\. Data cleaning convention

Data cleaning — dropping test users, capping outliers, correcting types, filling defaults — lives at the **tenant level** as dbt models that wrap staging tables. These are business rules shared across all workspaces on that tenant (e.g., "drop test user rows" is a property of the CommCare domain, not a single workspace). Multi-tenant workspaces that include a cleaned tenant will see the cleaned data.

Examples:

```sql
-- tenant model: cases_clean.sql (filtering)
SELECT * FROM {{ ref('stg_case_patient') }}
WHERE owner_name NOT IN ('test user 1', 'test user 2')

-- tenant model: visits_clean.sql (enrichment/capping)
SELECT *,
  LEAST(duration_minutes, 480) AS duration_minutes_capped
FROM {{ ref('stg_form_followup_visit') }}
```

When a cleaning model should be used *instead of* its upstream table for analysis, the `TransformationAsset` declares an explicit **`replaces`** relationship (see section 6). This tells the agent and UI: "prefer `cases_clean` over `stg_case_patient` for querying." If multiple cleaning layers are chained (`cases_no_test → cases_final`), the agent follows the `replaces` chain to the terminal model.

### 5\. Multi-tenant workspace data model

#### Purpose

Multi-tenant workspaces exist to provide a **shared analytical context** — the ability to query across multiple tenants (potentially from different providers). They are not about merging rows into unified tables.

#### Namespace strategy

**OPEN QUESTION**: This design is pending review. The current codebase uses `WorkspaceViewSchema` with UNION ALL views across matching table names. The proposed change below replaces that with per-tenant namespaced views. Awaiting confirmation on whether the original UNION approach was intentional or primarily for collision avoidance.

Each tenant's tables are exposed in the workspace view schema under a human-readable prefix using the tenant's `canonical_name`:

| Tenant | Table | Workspace view name |
| :---- | :---- | :---- |
| Malawi CommCare | `stg_case_patient` | `malawi_commcare__stg_case_patient` |
| Malawi Connect | `stg_form_visit` | `malawi_connect__stg_form_visit` |

- No implicit UNION ALL across tenants  
- Double underscore (`__`) separates tenant name from table name (consistent with repeat group naming)  
- Cross-tenant composition is done explicitly in workspace-level dbt models  
- The agent can suggest or generate cross-tenant joins

#### PostgreSQL isolation

The existing data isolation model is preserved:

- Each tenant schema has a read-only role (`{schema_name}_ro`)  
- Multi-tenant workspaces use `WorkspaceViewSchema` — a separate PG schema containing thin aliasing views that reference the underlying tenant schemas  
- The agent queries only within the workspace view schema via `SET search_path`  
- The agent cannot discover or access schemas it hasn't been granted

Single-tenant workspaces continue to bypass the view schema and query the tenant schema directly.

### 6\. Store transformations as first-class assets

Transformations are first-class persisted assets in a new Django app: `apps/transformations/`.

#### `TransformationAsset`

| Field | Type | Description |
| :---- | :---- | :---- |
| `id` | UUID | Primary key |
| `name` | CharField | dbt model name (must be unique within scope+container) |
| `description` | TextField | Human-readable description |
| `scope` | Enum: `system`, `tenant`, `workspace` | Determines visibility and editability |
| `tenant` | FK(Tenant), nullable | Set when scope is `system` or `tenant` |
| `workspace` | FK(Workspace), nullable | Set when scope is `workspace` |
| `sql_content` | TextField | The dbt model SQL (using `ref()` syntax within scope, direct table names across scopes) |
| `replaces` | FK(self), nullable | The `TransformationAsset` this model supersedes for querying |
| `test_yaml` | TextField, nullable | dbt schema test YAML |
| `created_by` | FK(User), nullable | Null for system-generated |
| `created_at` | DateTimeField |  |
| `updated_at` | DateTimeField |  |

**Constraints:**

- DB check constraint: exactly one of `tenant` or `workspace` is non-null  
- `scope=system` or `scope=tenant` → `tenant` is set, `workspace` is null  
- `scope=workspace` → `workspace` is set, `tenant` is null  
- `scope=system` → not editable by users (enforced in application logic)

**`replaces` semantics:**

- Self-referential FK to another `TransformationAsset`  
- System staging models have `replaces=None` (they are built from raw tables)  
- Tenant cleaning models typically replace a system staging model  
- Chains are followed to the terminal model for agent context

#### `TransformationRun`

Pipeline-level execution record for a full refresh cycle.

| Field | Type | Description |
| :---- | :---- | :---- |
| `id` | UUID | Primary key |
| `tenant` | FK(Tenant) | The tenant being refreshed |
| `workspace` | FK(Workspace), nullable | Set if workspace models were included |
| `status` | Enum: `pending`, `running`, `completed`, `failed` |  |
| `started_at` | DateTimeField |  |
| `completed_at` | DateTimeField, nullable |  |
| `error_message` | TextField, nullable |  |

#### `TransformationAssetRun`

Per-model execution record within a pipeline run.

| Field | Type | Description |
| :---- | :---- | :---- |
| `id` | UUID | Primary key |
| `run` | FK(TransformationRun) | Parent pipeline run |
| `asset` | FK(TransformationAsset) | The model that was executed |
| `status` | Enum: `pending`, `running`, `success`, `failed`, `skipped` |  |
| `duration_ms` | IntegerField, nullable |  |
| `logs` | TextField, nullable | dbt output for this model |
| `test_results` | JSONField, nullable | Pass/fail per test |
| `started_at` | DateTimeField |  |
| `completed_at` | DateTimeField, nullable |  |

#### System asset creation

System-scoped `TransformationAsset` records are created/updated **at materialization time** — after the DISCOVER phase generates metadata and before dbt runs. The materializer upserts asset records for each system model it will execute (one per case type, form, repeat group). This keeps asset records in sync with what actually exists in the database.

### 7\. Agent context engineering for layer preference

The agent sees only **terminal models** by default — models that are not replaced by anything downstream. This is resolved at system-prompt-build time:

1. Query all `TransformationAsset` records for the workspace's tenant(s)  
2. Follow all `replaces` chains to find terminal models (models where no other asset has `replaces` pointing to them)  
3. Include only terminal models in the system prompt schema context  
4. Raw tables and replaced intermediate models are omitted

The system prompt includes a note: "These tables are the result of a transformation pipeline. If the user asks about data lineage, raw data, or how transformations were applied, use the `get_lineage` tool."

**`get_lineage` MCP tool**: Given a model name, returns the full `replaces` chain back to raw, with descriptions at each step. This provides progressive discoverability — the agent can explore the full lineage when the user asks about it, without cluttering the default context.

Raw and intermediate tables remain queryable (the agent can `SELECT` from them after discovering them via lineage), but the agent will not use them by default.

### 8\. Permissions

- **System models**: not editable by users. Created and managed by the materializer.  
- **Tenant models**: any user with a `TenantMembership` for that tenant can create and edit tenant-level transformation assets. Tenant models affect all workspaces using that tenant — this is understood and accepted for v1.  
- **Workspace models**: governed by existing `WorkspaceMembership` roles. `read_write` and `manage` roles can author workspace-level assets.

### 9\. Authoring UX

The transformation authoring editor (CodeMirror-based) provides:

- SQL editing with `ref()` autocomplete — the editor knows all available model names within the current scope and offers them as completions  
- Autocomplete for direct table names when referencing cross-scope models (e.g., workspace model referencing tenant tables)  
- dbt schema test YAML editing  
- Manual run trigger for testing authored models  
- Lineage visualization showing the `replaces` chain and upstream dependencies

### 10\. Product surfacing and phasing

Phase the work:

1. **Backend architecture** Provider contract, CommCare staging models (dynamic generation from metadata), `apps/transformations/` Django app with `TransformationAsset`/`TransformationRun`/`TransformationAssetRun`, three-stage dbt execution, lineage exposure via `get_lineage` MCP tool, agent context engineering for terminal model preference  
2. **Management UX** Browse raw, staging, tenant, and workspace models together; see lineage, run status, and per-model test results  
3. **Authoring UX** CodeMirror SQL editor for dbt models (with `ref()` and table name autocomplete), YAML editor for schema tests, manual run trigger  
4. **AI-assisted authoring** (future) Users create and iterate on dbt models through Scout's existing chat interface — the agent generates dbt SQL with `ref()` that the user reviews and saves as a transformation asset

The data dictionary area should become the main discovery/manage surface for system tables plus transformation assets, but canonical persistence stays in `apps/transformations/`.

## Important Interfaces and Behavior

- Provider interface must separate `ingest`, `discover_metadata`, and `standardize`  
- System staging models are dynamically generated from provider metadata (one per case type, form, repeat group for CommCare)  
- System staging models are `TransformationAsset` records with `scope=system`, created at materialization time  
- Tenant models are visible anywhere that tenant is used (including multi-tenant workspaces)  
- Workspace models are only visible in that workspace  
- Any user with `TenantMembership` may author tenant-shared models  
- Within a scope: models use `ref()` for dependencies (dbt resolves the DAG)  
- Across scopes: models use direct table names (three-stage execution guarantees ordering)  
- Test results are surfaced per-model via `TransformationAssetRun`  
- Multi-tenant workspaces expose tenant tables under `{canonical_name}__{table_name}` namespace (see open question in section 5\)

## Test Plan

Cover these scenarios in the eventual implementation plan:

- CommCare standardization generates one staging table per case type and per form from raw payloads and metadata  
- Repeat groups generate child tables with FK back to parent form  
- Dynamic SQL generation from metadata produces valid dbt models  
- `TransformationAsset` records are created/updated at materialization time for system models  
- Tenant models run automatically after refresh and are visible across workspaces sharing the same tenant  
- Workspace models run after tenant models and can reference tables from multiple attached tenants via direct table names  
- dbt resolves execution order correctly via the DAG within each scope stage  
- `ref()` dependencies within a scope work (tenant model B → tenant model A)  
- Three-stage execution runs in order: system → tenant → workspace  
- Tenant cleaning models with `replaces` are preferred by the agent over the upstream staging table  
- Chained `replaces` resolves to the terminal model; agent system prompt includes only terminal models  
- `get_lineage` tool returns the full `replaces` chain with descriptions  
- Failed custom model runs do not fail the raw/system refresh  
- dbt schema tests run after model builds and results are logged per-model in `TransformationAssetRun`  
- Derived models never overwrite raw or system-owned staging tables  
- Permissions enforce:  
  - Any user with `TenantMembership` can author tenant-shared models  
  - Workspace-local models stay isolated to the workspace  
  - System models are not user-editable  
- Lineage/status APIs correctly report model scope, upstream dependencies, and test results  
- Multi-tenant workspace view schema creates namespaced views (`{canonical_name}__{table}`)  
- Data isolation: agent cannot access schemas outside its workspace context

## Assumptions and Defaults

- Architecture-first spec, not an implementation plan  
- dbt (already partially integrated) is the transformation engine for all layers  
- CommCare is the first fully specified provider; Connect is included as a near-term provider under the same contract but with less detail  
- Custom authored surface is dbt SQL models only for now; no macros or Python models  
- CommCare standardization is intentionally opinionated and fully relationalized (one table per case type/form, child tables for repeat groups)  
- Transformations are first-class assets in `apps/transformations/`, surfaced alongside the data dictionary  
- Custom transformations are additive only — new tables/views, never editing raw or staging  
- Agent sees only terminal models by default; progressive lineage discovery via `get_lineage` tool  
- Metadata source for CommCare staging is app JSON only (no raw data introspection) for v1  
- Schema evolution (handling removed properties/case types between refreshes) is deferred

## Open Questions

- **Multi-tenant namespace strategy** (Section 5): The proposed design replaces the current UNION ALL approach with per-tenant namespaced views. Pending confirmation on whether the original UNION design was intentional or primarily for collision avoidance.

## Future Considerations (Deferred)

The following are explicitly out of scope for v1 but noted for future iterations:

- **Versioning and draft/publish semantics**: revision history, draft vs published states, dual run modes, last-known-good output preservation on failure. Not needed until multi-user editing and production stability guarantees are required.  
- **Development environments**: duplicated workspaces for safe iteration on SQL changes before going live.  
- **dbt macros and Python models**: user-authored reusable macros or Python-based transformations.  
- **Schema evolution**: handling removed properties, case types, or forms between refreshes (additive-only vs. column/table cleanup).  
- **Tenant-level permissions**: role-based access control for who can author tenant-level transformations (currently any `TenantMembership` holder can author).
