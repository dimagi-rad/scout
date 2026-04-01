# Tenant and Workspace Transformation Architecture (v2)

## Summary

Design a layered data architecture that moves Scout from raw JSON-heavy source tables to a managed analytical model using dbt as the transformation engine. Three authored transformation scopes:

- System-owned provider standardization (staging layer)
- Tenant-shared custom dbt models
- Workspace-local custom dbt models

dbt manages execution order via its DAG (built from `ref()` dependencies), so models do not need to be manually ordered. Each `.sql` file is one dbt model producing one table or view. All custom transformations run automatically on data refresh. CommCare is the first fully specified provider; Connect fits the same contract but does not need equal detail in this first spec.

## Key Changes

### 1. Introduce a provider contract with a standardized system layer

Define a provider pipeline contract with distinct responsibilities:

- Raw ingestion: fetch and preserve source payloads with provenance (`raw_*` tables)
- Metadata discovery: fetch schema-defining metadata needed to interpret the source
- Standardization: generate stable, typed, relational analytical tables from raw + metadata (`stg_*` tables)

For v1 of the architecture, fully flesh this out for CommCare:
- Persist raw forms, raw cases, raw app/schema metadata as `raw_forms`, `raw_cases`
- Persist metadata that maps forms/questions/repeats/case types into a stable source model
- Generate system-owned staging models (`stg_cases`, `stg_forms`) — typed, flattened, relational — so downstream dbt models and the agent do not need to reason through nested JSON blobs

Connect should be named in the spec as a supported future implementation of the same contract, with the note that its exact sourcing strategy vs CommCare can be decided later.

### 2. Use dbt as the transformation engine

All transformation layers — system staging, tenant, and workspace — are dbt models:

- **One model per `.sql` file**, each producing one table or view
- **`ref()` for dependencies**: models reference upstream models via `{{ ref('model_name') }}`, not hardcoded table names
- **DAG-based execution**: dbt resolves the dependency graph automatically; no manual ordering needed
- **Additive only**: custom models create new relations; they may not overwrite raw or system-owned staging tables

#### dbt layering convention

| Layer | Prefix/convention | What it does | Visibility |
|-------|-------------------|-------------|------------|
| Raw | `raw_*` | Exact source payloads from providers | Audit trail; agent deprioritizes |
| Staging | `stg_*` | System standardization — type casting, JSON flattening, renaming | Agent + all users (clean base) |
| Tenant | User-named | Shared business rules, exclusions, feature generation, enrichment | Agent + all workspaces on that tenant |
| Workspace | User-named | Workspace-specific joins, rollups, cross-tenant compositions, derived marts | Agent + that workspace only |

Models within the same scope can `ref()` each other freely — dbt handles the ordering. Workspace models may also `ref()` tenant models. Tenant models `ref()` staging models.

### 3. Add data quality tests

dbt's YAML-based testing framework provides data quality validation at each layer:

- **Schema tests** defined in `.yml` files alongside models: `unique`, `not_null`, `accepted_values`, `relationships`
- Tests run after models are built during the transform phase
- Test failures are logged and surfaced in run status but do not block downstream models
- Users can author custom tests for their tenant and workspace models

Example schema test (in a `.yml` file alongside models):
```yaml
models:
  - name: stg_cases
    columns:
      - name: case_id
        tests:
          - unique
          - not_null
      - name: case_type
        tests:
          - not_null
```

### 4. Data cleaning convention

Data cleaning — dropping test users, capping outliers, correcting types, filling defaults — lives at the **tenant level** as dbt models that wrap staging tables. These are business rules shared across all workspaces on that tenant (e.g., "drop test user rows" is a property of the CommCare domain, not a single workspace).

Examples:
```sql
-- tenant model: cases_clean.sql (filtering)
SELECT * FROM {{ ref('stg_cases') }}
WHERE owner_name NOT IN ('test user 1', 'test user 2')

-- tenant model: visits_clean.sql (enrichment/capping)
SELECT *,
  LEAST(duration_minutes, 480) AS duration_minutes_capped
FROM {{ ref('stg_visits') }}
```

When a cleaning model should be used *instead of* its upstream table for analysis, the `TransformationAsset` declares an explicit **`replaces`** relationship (see section 5). This tells the agent and UI: "prefer `cases_clean` over `stg_cases` for querying." If multiple cleaning layers are chained (`cases_no_test → cases_final`), the agent follows the `replaces` chain to the terminal model.

### 5. Agent context engineering for layer preference

The agent should prefer the most derived layer available when querying:

- If a model declares `replaces` on a staging table, the agent uses the replacement
- If multiple `replaces` are chained, follow to the terminal model
- If a workspace model exists that refines a tenant model, prefer the workspace model
- If only staging exists, use staging; never default to raw

This is implemented via the agent's system prompt and the `replaces` metadata — not by hiding tables. Raw tables remain queryable for debugging and auditing, but the agent is instructed to deprioritize them.

Lineage must be exposed so the agent and UI can distinguish:
  raw table → system staging model → tenant-derived model (possibly replacing staging) → workspace-derived model

### 6. Store transformations as first-class assets

Do not store executable SQL inside the data dictionary payload. Instead, add explicit persisted assets, surfaced in the same product area as the data dictionary.

Required conceptual models/interfaces:
- `TransformationAsset`
  - Scope: `tenant` or `workspace`
  - Container owner: tenant or workspace
  - Metadata: name, description, created_by
  - Content: the dbt model SQL (using `ref()` syntax)
  - **`replaces`**: optional FK to another table/model this asset supersedes for querying purposes
  - Associated schema tests (YAML)
- `TransformationRun`
  - Trigger: refresh (automatic)
  - Scope, status, logs/errors, test results, timestamps

On data refresh, the pipeline runs: raw ingestion → system staging → tenant models → workspace models → tests. All via dbt.

### 7. Product surfacing and phasing

Phase the work:

1. **Backend architecture**
   Provider contract, CommCare staging models, transformation asset storage, dbt execution integration, lineage exposure, test runner integration
2. **Management UX**
   Browse raw, staging, tenant, and workspace models together; see lineage, run status, and test results
3. **Authoring UX**
   SQL editor for dbt models (with `ref()` support), YAML editor for schema tests, manual run trigger
4. **AI-assisted authoring** (future)
   Users create and iterate on dbt models through Scout's existing chat interface — the agent generates dbt SQL with `ref()` that the user reviews and saves as a transformation asset

The data dictionary area should become the main discovery/manage surface for system tables plus transformation assets, but canonical persistence stays in dedicated transformation models.

## Important Interfaces and Behavior

- Provider interface must separate `ingest`, `discover_metadata`, and `standardize`
- Standardized staging models must be system-owned and stable enough to serve as the base contract for authored dbt models
- Tenant models are visible anywhere that tenant is used
- Workspace models are only visible in that workspace
- Any write-capable user may author tenant-shared models
- Workspace models may `ref()` tenant models
- Models within the same scope may `ref()` each other (dbt resolves the DAG)
- Test results are surfaced per-model in run status

## Test Plan

Cover these scenarios in the eventual implementation plan:

- CommCare standardization generates staging models from raw payloads and metadata without requiring custom SQL
- Tenant models run automatically after refresh and are visible across workspaces sharing the same tenant
- Workspace models run after tenant models and can reference models from multiple attached tenants
- dbt resolves execution order correctly via the DAG — no manual ordering
- `ref()` dependencies across scopes work (workspace → tenant → staging)
- `ref()` dependencies within a scope work (tenant model B → tenant model A)
- Tenant cleaning models with `replaces` are preferred by the agent over the upstream staging table
- Chained `replaces` resolves to the terminal model
- Failed custom model runs do not fail the raw/system refresh
- dbt schema tests run after model builds and results are logged
- Derived models never overwrite raw or system-owned staging tables
- Permissions enforce:
  - Any write user can author tenant-shared models
  - Workspace-local models stay isolated to the workspace
- Lineage/status APIs correctly report model scope, upstream dependencies, and test results

## Assumptions and Defaults

- Architecture-first spec, not an implementation plan
- dbt (already partially integrated) is the transformation engine for all layers
- CommCare is the first fully specified provider; Connect is included as a near-term provider under the same contract but with less detail
- Custom authored surface is dbt SQL models only for now; no macros or Python models
- CommCare standardization is intentionally opinionated and fully relationalized
- Transformations are first-class assets surfaced alongside the data dictionary, not embedded inside it
- Custom transformations are additive only — new tables/views, never editing raw or staging
- Agent prefers derived layers via context engineering; raw tables remain accessible but deprioritized

## Future Considerations (Deferred)

The following are explicitly out of scope for v1 but noted for future iterations:

- **Versioning and draft/publish semantics**: revision history, draft vs published states, dual run modes, last-known-good output preservation on failure. Not needed until multi-user editing and production stability guarantees are required.
- **Development environments**: duplicated workspaces for safe iteration on SQL changes before going live.
- **dbt macros and Python models**: user-authored reusable macros or Python-based transformations.
