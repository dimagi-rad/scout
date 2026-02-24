# Data Explorer MCP — Implementation TODO

Tracks remaining work against the design in `data-explorer-mcp-design.md`.

---

## MCP Tools

- [x] **`teardown_schema` tool** — expose `SchemaManager.teardown()` as an MCP tool; `requires_confirmation` parameter per design
- [x] **`get_materialization_status` tool** — query `MaterializationRun` by run ID; enables reconnect-and-poll fallback
- [x] **`list_pipelines` tool** — list available pipelines from the registry with descriptions
- [x] **`cancel_materialization` tool** — marks in-progress runs as failed (best-effort; subprocess cancellation deferred)

---

## Materialization Pipeline

- [x] **Pipeline Registry** — YAML-based pipeline definitions (`pipelines/commcare_sync.yml`) with sources, loader references, DBT model list
- [x] **Three-phase structure** — Discover → Load → Transform phases with per-phase state tracking in `MaterializationRun`
- [x] **Discover phase** — CommCare metadata loader (app definitions, case types, form structure); stored in generic `TenantMetadata` model (django-pydantic-field)
- [x] **Forms loader** — paginated CommCare form submission loader with nested case-reference extraction (`loaders/commcare_forms.py`)
- [ ] **Users loader** — CommCare user loader (`loaders/commcare/users.py`)
- [x] **DBT integration** — runtime `profiles.yml` generation, programmatic `dbtRunner` API, threading.Lock for concurrency safety
- [x] **MCP progress notifications** — `ctx.report_progress` with `asyncio.run_coroutine_threadsafe`; done-callback for silent failure logging
- [ ] **Cancellation support** — handle MCP `cancelled` notifications; terminate active loader/DBT subprocesses gracefully (deferred)

---

## Metadata Service

- [x] **Tenant semantic metadata models** — generic `TenantMetadata` model (provider-agnostic JSON field, persists across schema teardown)
- [ ] **Pipeline-driven `list_tables`** — replace `information_schema` introspection with pipeline registry + `MaterializationRun` records; include row counts and `materialized_at` timestamps
- [ ] **Pipeline-driven `describe_table`** — merge pipeline column definitions with tenant-specific field descriptions from the discover phase output
- [ ] **`get_metadata` enrichment** — include table relationships defined by the pipeline

---

## Security

- [ ] **PostgreSQL role isolation** — create per-tenant DB roles (`role_{tenant}`), grant schema-scoped `USAGE`+`SELECT`, use `SET ROLE` at query time instead of relying on `search_path` alone
- [ ] **Append-only audit DB table** — `MCPAuditLog` Django model (user ID, tenant ID, tool, args redacted, status, timing); replace logger-only audit trail
- [ ] **Network isolation for loaders** — restrict loader subprocess egress to configured API endpoints only

---

## Background Execution

- [ ] **Celery workers** — run materialization in background tasks so the MCP tool call can stream progress without blocking; result retrievable via `get_materialization_status`
- [ ] **Long-run resilience** — ensure `MaterializationRun` captures enough state that a reconnecting agent can get the final result even if the original connection dropped

---

## Completed

- [x] `TenantMembership` model and CommCare domain resolution
- [x] `TenantCredential` model (OAuth + API key, Fernet encryption)
- [x] `TenantSchema` model with full state machine
- [x] `MaterializationRun` model
- [x] `SchemaManager.provision()` and `teardown()` (schema-level, not yet exposed as tool)
- [x] `list_tables` tool (live `information_schema` introspection)
- [x] `describe_table` tool (live `information_schema` introspection)
- [x] `get_metadata` tool (live `information_schema` introspection)
- [x] `query` tool with SQL validation, row limit, statement timeout
- [x] `run_materialization` tool (synchronous, cases only)
- [x] CommCare Case API v2 loader with cursor pagination
- [x] Consistent response envelope (`success_response` / `error_response`)
- [x] Audit logging to `mcp_server.audit` logger
- [x] `stdio` and `streamable-http` transports
- [x] Pass-through auth via `TenantCredential` (OAuth + API key paths)
- [x] Token refresh service (`apps/users/services/token_refresh.py`)
- [x] `get_schema_status` tool — check schema existence without triggering correction loop
- [x] Automatic materialization via agent system prompt (Data Availability section)
- [x] `teardown_schema` tool with `confirm=True` guard
