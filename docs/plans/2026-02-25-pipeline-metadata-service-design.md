# Pipeline-Driven Metadata Service Design

**Date:** 2026-02-25
**Status:** Approved

## Overview

Replace `information_schema` introspection in `list_tables`, `describe_table`, and `get_metadata` with a pipeline-aware metadata service. The new service enriches responses with row counts, materialization timestamps, CommCare semantic context from the discover phase, and table relationships from the pipeline registry.

## Goals

- `list_tables` returns row counts and `materialized_at` timestamps from `MaterializationRun` records
- `describe_table` annotates JSONB columns with human-readable summaries derived from `TenantMetadata` (the discover phase output)
- `get_metadata` includes table relationships defined in the pipeline YAML
- Empty list returned when no completed `MaterializationRun` exists (no fallback to `information_schema` for `list_tables`)
- JSONB annotation is best-effort — degrades gracefully if `TenantMetadata` is absent

## Non-Goals

- Expanding JSONB columns into virtual fields
- Static column definitions in the YAML
- Changing `query` tool behavior

---

## Part 1: Pipeline YAML Extension

Add a `relationships` block to `commcare_sync.yml`:

```yaml
relationships:
  - from_table: forms
    from_column: case_ids
    to_table: cases
    to_column: case_id
    description: "Form submissions reference the cases they update (case_ids is a JSON array)"
```

### Registry Changes

New dataclass in `pipeline_registry.py`:

```python
@dataclass
class RelationshipConfig:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    description: str = ""
```

`PipelineConfig` gains:

```python
relationships: list[RelationshipConfig] = field(default_factory=list)
```

The `_parse_pipeline` function reads the `relationships` list the same way it reads `sources`.

---

## Part 2: `mcp_server/services/metadata.py`

New module with three functions. All are synchronous; callers use `sync_to_async` as needed.

### `pipeline_list_tables`

```python
def pipeline_list_tables(
    tenant_schema: TenantSchema,
    pipeline_config: PipelineConfig,
) -> list[dict]:
```

1. Query the latest completed `MaterializationRun` for `tenant_schema`
2. Return `[]` if none found
3. Build entries from:
   - Pipeline sources → row counts from `run.result["sources"][name]["rows"]`
   - DBT models → from `pipeline_config.dbt_models`, row_count=None
4. `materialized_at` comes from `run.completed_at.isoformat()`

Response shape per entry:
```json
{
  "name": "cases",
  "type": "table",
  "description": "CommCare case records",
  "row_count": 4823,
  "materialized_at": "2026-02-24T10:00:00Z"
}
```

DBT model entries use `row_count: null` since the transform phase doesn't track per-model counts.

### `pipeline_describe_table`

```python
def pipeline_describe_table(
    table_name: str,
    schema_name: str,
    conn,
    tenant_metadata: TenantMetadata | None,
    pipeline_config: PipelineConfig,
) -> dict | None:
```

1. Query `information_schema.columns` — returns `None` if table not found
2. Add table-level `description` from matching pipeline source (empty string if not found)
3. For each column, add `description: ""`
4. Enrich JSONB columns from `TenantMetadata.metadata` if available:
   - `properties` column → "Contains case properties. Available case types: {comma-separated case type names}"
   - `form_data` column → "Contains form submission data. Available forms: {comma-separated form names}"
5. If `TenantMetadata` is absent or metadata is empty, JSONB columns get `description: ""`

### `pipeline_get_metadata`

```python
def pipeline_get_metadata(
    tenant_schema: TenantSchema,
    schema_name: str,
    conn,
    tenant_metadata: TenantMetadata | None,
    pipeline_config: PipelineConfig,
) -> dict:
```

1. Call `pipeline_list_tables` — if empty, return `{"tables": {}, "relationships": []}`
2. Call `pipeline_describe_table` for each table
3. Return `{"tables": {...}, "relationships": [...]}`

`relationships` is serialized from `pipeline_config.relationships`:
```json
{
  "from_table": "forms",
  "from_column": "case_ids",
  "to_table": "cases",
  "to_column": "case_id",
  "description": "..."
}
```

---

## Part 3: `server.py` Changes

### `list_tables` tool

Replace `_tenant_list_tables` call with:
1. Fetch `TenantSchema` for the tenant (via `TenantMembership`) — if none, return empty list
2. Resolve `pipeline_config` from the registry using the pipeline name from the last `MaterializationRun` (default: `commcare_sync`)
3. Call `await sync_to_async(pipeline_list_tables)(tenant_schema, pipeline_config)`
4. If result is empty, include a `note` in the response: `"No completed materialization run found. Run run_materialization to load data."`

### `describe_table` tool

Replace `_tenant_describe_table` call with:
1. Fetch `TenantSchema` and `TenantMetadata` for the tenant
2. Call `pipeline_describe_table` using a managed DB connection
3. `TenantMetadata` may be `None` — the function handles it gracefully

### `get_metadata` tool

Replace the loop over `_tenant_list_tables` / `_tenant_describe_table` with:
1. Fetch `TenantSchema` and `TenantMetadata`
2. Call `pipeline_get_metadata`
3. Add `relationships` to the top-level response

### Cleanup

Remove `_tenant_list_tables` and `_tenant_describe_table` helper functions once all three tools are migrated.

---

## Part 4: Response Shape Changes

### `list_tables`
```json
{
  "tables": [
    {
      "name": "cases",
      "type": "table",
      "description": "CommCare case records",
      "row_count": 4823,
      "materialized_at": "2026-02-24T10:00:00Z"
    },
    {
      "name": "stg_cases",
      "type": "table",
      "description": "",
      "row_count": null,
      "materialized_at": "2026-02-24T10:00:00Z"
    }
  ],
  "note": null
}
```

### `describe_table`
```json
{
  "name": "cases",
  "description": "CommCare case records",
  "columns": [
    {"name": "case_id", "type": "text", "nullable": false, "default": null, "description": ""},
    {"name": "properties", "type": "jsonb", "nullable": true, "default": null,
     "description": "Contains case properties. Available case types: pregnancy, child, household"}
  ]
}
```

### `get_metadata`
```json
{
  "schema": "tenant_abc",
  "table_count": 4,
  "tables": { ... },
  "relationships": [
    {
      "from_table": "forms",
      "from_column": "case_ids",
      "to_table": "cases",
      "to_column": "case_id",
      "description": "Form submissions reference the cases they update (case_ids is a JSON array)"
    }
  ]
}
```

---

## Key Design Decisions

1. **`list_tables` returns empty on no completed run** — no fallback to `information_schema`. Agents should call `run_materialization` first. The `note` field surfaces this clearly.

2. **`describe_table` keeps `information_schema` for column structure** — the pipeline YAML doesn't define columns. Only descriptions are derived from the discover phase.

3. **JSONB annotation is a string summary, not virtual fields** — the JSONB column gets an annotated `description` field. Individual subkeys are not expanded into separate column entries.

4. **`TenantMetadata` absence is graceful** — if discovery hasn't run, JSONB columns get empty descriptions. No error.

5. **Relationships live in the pipeline YAML** — they're static declarations, not inferred at runtime.
