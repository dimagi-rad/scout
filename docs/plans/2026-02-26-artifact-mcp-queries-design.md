# Design: Artifact Live Query Execution via MCP (Issue #19, Option A)

## Problem

Artifacts created with `source_queries` (SQL queries for live data) render with empty/zero data. The root cause: `ArtifactQueryDataView` was stubbed out to return `queries: []` when direct DB access was removed. The sandbox withholds `artifact.data` when `has_live_queries` is true, expecting a live fetch — so these artifacts get nothing.

## Approach

Reuse `mcp_server` query execution code directly from the Django view rather than making an HTTP round-trip to the MCP server. The `mcp_server` module lives in the same Python project, so `load_tenant_context` and `execute_query` can be imported and called inline. This is the same code path the MCP `query` tool uses.

## Data Flow

```
GET /api/artifacts/{id}/query-data/
  → Auth + workspace membership check
  → load artifact.source_queries: [{name, sql}, ...]
  → load_tenant_context(artifact.workspace.tenant_id)
  → for each query: execute_query(ctx, sql)
  → return {
      "queries": [{"name": ..., "columns": [...], "rows": [...]}],
      "static_data": artifact.data or {}
    }
```

The sandbox `mergeQueryResults()` already handles this shape — no frontend changes needed.

## Changes

### `apps/artifacts/views.py` — `ArtifactQueryDataView`

- Convert `.get()` to `async` (app runs ASGI/uvicorn already)
- Add workspace membership access check (currently absent)
- Load `tenant_id` from `artifact.workspace.tenant_id`; return 400 if workspace is None
- Call `load_tenant_context(tenant_id)` — return error response on `ValueError` (no active schema)
- For each entry in `artifact.source_queries`:
  - Execute via `execute_query(ctx, entry["sql"])`
  - On error, include `{"name": ..., "error": "..."}` and continue
- Return `{"queries": [...], "static_data": artifact.data or {}}`

### `apps/artifacts/tests/test_artifact_query_data.py` (new)

- Happy path: queries run and return correct column/row structure
- No workspace → 400
- No active schema → graceful error per query
- Per-query failure → error entry, other queries succeed
- Unauthenticated → 401
- Non-member → 403

## Out of Scope

- No frontend changes
- No model/migration changes
- `ArtifactSandboxView` `has_live_queries` logic unchanged
- `SharedArtifactView` unaffected
