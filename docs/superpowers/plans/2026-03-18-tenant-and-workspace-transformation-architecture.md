# Tenant and Workspace Transformation Architecture

## Summary

Design a layered data architecture that moves Scout from raw JSON-heavy source tables to a managed analytical model with three authored transformation scopes:

- System-owned provider standardization
- Tenant-shared custom SQL transformations
- Workspace-local custom SQL transformations

The architecture should support both automatic execution on refresh for published revisions and manual execution for drafts, while preserving the last successful published outputs when a later run fails. CommCare is the first fully specified provider; Connect is called out as a near-term peer provider and should fit the same contract, but does not need equal detail in this first spec.

## Key Changes

### 1. Introduce a provider contract with a standardized system layer
Define a provider pipeline contract with distinct responsibilities:

- Raw ingestion: fetch and preserve source payloads with provenance
- Metadata discovery: fetch schema-defining metadata needed to interpret the source
- Standardization: generate stable, typed, relational analytical tables from raw + metadata

For v1 of the architecture, fully flesh this out for CommCare:
- Persist raw forms, raw cases, raw app/schema metadata
- Persist metadata that maps forms/questions/repeats/case types into a stable source model
- Generate system-owned typed relations so downstream SQL and the agent do not need to reason through nested JSON blobs

Connect should be named in the spec as a supported future implementation of the same contract, with the note that its exact sourcing strategy vs CommCare can be decided later.

### 2. Add two additive custom transformation layers
Use two user-authored SQL layers above the system layer:

- Tenant layer: shared cleanup, enrichment, feature generation, canonical exclusions, reusable business rules for one underlying tenant
- Workspace layer: workspace-only joins, filters, cross-tenant compositions, derived marts, and analysis-specific rollups

Custom layers are additive only:
- They may create new derived relations
- They may not rewrite or replace raw/system-owned standardized relations

Execution order:
- Raw ingestion
- System standardization
- Published tenant transformations
- Published workspace transformations

### 3. Store transformations as first-class versioned assets
Do not store executable SQL inside the data dictionary payload. Instead, add explicit persisted assets, surfaced in the same product area as the data dictionary.

Required conceptual models/interfaces:
- `TransformationAsset`
  Scope: `tenant` or `workspace`
  Container owner: tenant or workspace
  Metadata: name, description, status, created_by
- `TransformationRevision`
  Immutable SQL revision linked to an asset
  States: draft, published, archived/superseded
- `TransformationRun`
  Trigger: refresh or manual
  Target revision(s), scope, status, logs/errors, materialized outputs, timestamps
- `PublishedOutputPointer` or equivalent
  Tracks the last successful published revision/output per asset scope for stable queryability

### 4. Use draft/publish semantics with dual run modes
Support both:
- Automatic runs on refresh for published revisions only
- Manual runs for drafts and experimentation

Failure policy:
- A failed custom transformation run does not fail the raw/system refresh
- Previously successful published derived outputs remain queryable
- The failed run is surfaced separately in status/history

### 5. Product surfacing and phasing
Phase the work in the spec:

1. Backend architecture
   Provider contract, CommCare standardization, transformation assets, execution/runtime, lineage exposure
2. Management UX
   Browse system tables and custom models together, see lineage/run status, publish drafts
3. Authoring UX
   SQL editor, validation, draft/manual run flow
4. AI-assisted authoring
   Natural-language-to-SQL assistance that produces explicit user-reviewable SQL revisions

The data dictionary area should become the main discovery/manage surface for system tables plus transformation assets, but canonical persistence stays in dedicated transformation models.

## Important Interfaces and Behavior

- Provider interface must separate `ingest`, `discover_metadata`, and `standardize`
- Standardized relations must be system-owned and stable enough to serve as the base contract for authored SQL
- Tenant transforms are visible anywhere that tenant is used
- Workspace transforms are only visible in that workspace
- Any write-capable user may publish tenant-shared transformations
- Workspace transforms may depend on tenant-shared outputs
- Lineage must be exposed so the agent and UI can distinguish:
  raw relation vs system-standardized relation vs tenant-derived model vs workspace-derived model

## Test Plan

Cover these scenarios in the eventual implementation plan:

- CommCare standardization generates relational outputs from raw payloads and metadata without requiring custom SQL
- Tenant published models run automatically after refresh and remain visible across workspaces sharing the same tenant
- Workspace published models run after tenant models and can join across multiple attached tenants
- Manual draft runs do not replace published outputs until explicitly published
- Failed custom runs preserve the last successful published outputs
- Derived models never overwrite raw/system-owned relations
- Permissions enforce:
  any write user can publish tenant-shared transforms
  workspace-local transforms stay isolated to the workspace
- Lineage/status APIs correctly report model scope, revision state, last successful run, and upstream dependencies

## Assumptions and Defaults

- Architecture-first spec, not an implementation plan
- CommCare is the first fully specified provider; Connect is included as a near-term provider under the same contract but with less detail
- Custom authored surface is SQL models only for now; no tenant-authored macros or Python models
- CommCare standardization is intentionally opinionated and fully relationalized
- Transformations are first-class assets surfaced alongside the data dictionary, not embedded inside it
- Custom transformations are additive only
- Published refresh uses last-known-good outputs on failure
- AI-assisted SQL authoring is planned, but not required for the first delivery slice
