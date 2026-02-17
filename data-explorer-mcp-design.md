# Data Explorer MCP Server — Design Document

**Author:** Simon  
**Date:** February 2026  
**Status:** Draft  

---

## 1. Overview

This document describes the design of an MCP (Model Context Protocol) server that provides a data exploration agent with secure, tenant-scoped access to databases. The server is responsible for:

- Authenticating users via OAuth 2.0 (or accepting pass-through credentials from the agent host)
- Provisioning isolated database schemas scoped to the user's tenant permissions
- Materializing data into those schemas by orchestrating pre-defined data loading and transformation pipelines (API ingestion + DBT)
- Exposing rich database metadata so the agent can reason about available data

The MCP server acts as a controlled gateway: the agent never gets raw database credentials. Instead, it interacts through well-defined MCP tools that enforce tenant boundaries at every layer.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Agent Host                          │
│  (Claude, LangGraph, or other MCP-compatible client)    │
└──────────────────────┬──────────────────────────────────┘
                       │ MCP Protocol (stdio / SSE)
                       │
┌──────────────────────▼──────────────────────────────────┐
│                 MCP Server                               │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ Auth Layer  │  │ Tool Router  │  │ Session Mgr    │  │
│  │ (OAuth2 /   │  │              │  │ (tenant ctx,   │  │
│  │  passthru)  │  │              │  │  schema refs)  │  │
│  └──────┬──────┘  └──────┬───────┘  └───────┬────────┘  │
│         │                │                   │           │
│  ┌──────▼────────────────▼───────────────────▼────────┐  │
│  │              Core Services                         │  │
│  │                                                    │  │
│  │  ┌──────────────┐  ┌───────────┐  ┌─────────────┐ │  │
│  │  │ Schema Mgr   │  │ Metadata  │  │ Materializer│ │  │
│  │  │ (provision,  │  │ Service   │  │ (loaders +  │ │  │
│  │  │  teardown)   │  │           │  │  DBT runner) │ │  │
│  │  └──────┬───────┘  └─────┬─────┘  └──────┬──────┘ │  │
│  └─────────┼────────────────┼────────────────┼────────┘  │
└────────────┼────────────────┼────────────────┼───────────┘
             │                │                │
     ┌───────▼────────────────▼────────────────▼───────┐
     │              Database (PostgreSQL)               │
     │                                                  │
     │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
     │  │ tenant_a │  │ tenant_b │  │ _catalog  │       │
     │  │ _user123 │  │ _user456 │  │ (metadata)│       │
     │  └──────────┘  └──────────┘  └──────────┘       │
     └─────────────────────────────────────────────────┘
```

### 2.1 Component Summary

| Component | Responsibility |
|---|---|
| **Auth Layer** | Validates OAuth 2.0 tokens or accepts pass-through auth headers from the agent host. Resolves the authenticated user to a tenant context. |
| **Session Manager** | Maintains per-session state: tenant ID, provisioned schema name, user permissions, materialization status. |
| **Schema Manager** | Creates and tears down tenant-scoped database schemas. Manages connection pools and schema-level access control. |
| **Metadata Service** | Introspects database catalogs and exposes table/column/relationship metadata to the agent. |
| **Materializer** | Orchestrates data loading from external APIs and runs DBT projects to transform raw data into analytics-ready tables. |
| **Tool Router** | Maps incoming MCP tool calls to the appropriate service, injecting the session's tenant context. |

---

## 3. Authentication

### 3.1 Auth Modes

The server supports two authentication modes, negotiated at connection time:

**Mode 1 — Direct OAuth 2.0 (Authorization Code + PKCE)**

Used when the MCP server is the relying party. The agent host initiates the OAuth flow on behalf of the user:

1. Agent host calls the `auth/start` resource to get the authorization URL.
2. User completes login in their browser and is redirected back.
3. Agent host passes the authorization code to the MCP server.
4. MCP server exchanges the code for tokens, validates them, and establishes a session.

This follows the emerging MCP authorization specification, where the MCP server itself acts as both the authorization server (or delegates to an upstream IdP) and the resource server.

**Mode 2 — Pass-through Token**

Used when the agent host has already authenticated the user (e.g., the host application has its own OAuth session). The host forwards the user's access token in the MCP request metadata:

```json
{
  "method": "tools/call",
  "params": {
    "name": "list_tables",
    "arguments": {},
    "_meta": {
      "authorization": "Bearer <token>"
    }
  }
}
```

The MCP server validates the token against the upstream IdP (via introspection endpoint or JWKS verification) and extracts tenant claims.

### 3.2 Token Claims and Tenant Resolution

Regardless of auth mode, the validated token must contain claims that map the user to a tenant:

```json
{
  "sub": "user_123",
  "tenant_id": "acme_corp",
  "scopes": ["data:read", "data:write", "schema:provision", "materialize:run"],
  "org_roles": ["analyst"]
}
```

The `tenant_id` claim determines which schemas the user can access or create. The `scopes` claim governs which MCP tools are available to the session.

### 3.3 Scope-to-Tool Mapping

| Scope | Permitted Tools |
|---|---|
| `data:read` | `list_databases`, `list_tables`, `describe_table`, `query`, `get_metadata` |
| `data:write` | `query` (with INSERT/UPDATE — if ever needed) |
| `schema:provision` | `provision_schema`, `teardown_schema` |
| `materialize:run` | `run_materialization`, `get_materialization_status` |
| `materialize:configure` | `list_pipelines`, `configure_pipeline` |

---

## 4. Schema Provisioning

### 4.1 Naming Convention

Each provisioned schema is namespaced to the tenant and session to prevent collisions and enforce isolation:

```
{tenant_id}_{user_id}_{purpose}
```

For example: `acme_corp_user123_exploration` or `acme_corp_user123_20260216`.

A catalog schema (`_catalog`) stores metadata about all provisioned schemas, their owners, creation timestamps, and lifecycle state.

### 4.2 Provisioning Flow

```
Agent calls provision_schema
       │
       ▼
┌─ Check user permissions (tenant_id, schema:provision scope) ─┐
│                                                               │
│  Existing active schema for this tenant/user/purpose?         │
│     │ yes                                                     │
│     ▼                                                         │
│  Refresh last_accessed_at, return existing schema ref         │
│  (data and materializations from prior sessions preserved)    │
│                                                               │
│     │ no                                                      │
│     ▼                                                         │
│  Check quota (max schemas per tenant, max per user)           │
│         │                                                     │
│         ▼                                                     │
│  CREATE SCHEMA with restricted GRANT                          │
│  - Schema owner: service role                                 │
│  - GRANT USAGE, SELECT to session role                        │
│  - SET search_path for session                                │
│         │                                                     │
│         ▼                                                     │
│  Register in _catalog                                         │
│         │                                                     │
│         ▼                                                     │
│  Return schema reference to agent                             │
└───────────────────────────────────────────────────────────────┘
```

### 4.3 Lifecycle and Cleanup

Schemas have a configurable TTL (default: 24 hours from last access). A background job inspects the `_catalog` table and drops expired schemas. The agent can also explicitly call `teardown_schema` to clean up immediately.

**Important:** Schema teardown only removes the data schema itself (tables, views, and raw staging data). Metadata loaded during materialization (e.g., semantic descriptions, column annotations, pipeline configuration snapshots) is stored separately in the `_catalog` or a dedicated `_metadata_{tenant_id}` schema and persists across schema lifecycle events. This means a re-provisioned schema can skip metadata ingestion if the tenant's metadata is already current.

Each schema tracks:

- `created_at` — when it was provisioned
- `last_accessed_at` — updated on every query or materialization
- `state` — one of `provisioning`, `active`, `materializing`, `expired`, `teardown`
- `materialization_run_id` — links to the most recent materialization, if any

### 4.4 Database-Level Isolation

The server uses PostgreSQL's role-based access control to enforce isolation. Each tenant gets a database role that can only access schemas belonging to that tenant. The MCP server connects using a service role and uses `SET ROLE` to assume the tenant role for query execution, ensuring that even a SQL injection through the agent cannot cross tenant boundaries.

```sql
-- Service role creates the schema
CREATE SCHEMA acme_corp_user123_exploration;

-- Tenant role gets scoped access
GRANT USAGE ON SCHEMA acme_corp_user123_exploration TO role_acme_corp;
GRANT SELECT ON ALL TABLES IN SCHEMA acme_corp_user123_exploration TO role_acme_corp;

-- At query time
SET ROLE role_acme_corp;
SET search_path TO acme_corp_user123_exploration;
```

---

## 5. Metadata Service

The metadata service gives the agent the context it needs to reason about available data without requiring it to write introspection queries directly.

### 5.1 Metadata Hierarchy

```
Database
 └── Schema (tenant-scoped)
      └── Table / View
           ├── Columns (name, type, nullable, default, description)
           ├── Primary Keys
           ├── Foreign Keys (references)
           ├── Indexes
           └── Row count estimate (from pg_stat)
```

### 5.2 Metadata Tools

**`list_databases`** — Returns databases the user's tenant has access to. In practice this may be a single shared database with schema-level isolation, or multiple databases for tenants with dedicated instances.

**`list_tables`** — Returns all tables and views in the current schema, with row count estimates and short descriptions.

```json
{
  "tables": [
    {
      "name": "customers",
      "type": "table",
      "row_count_estimate": 15420,
      "description": "Core customer records synced from CRM",
      "materialized_at": "2026-02-16T10:30:00Z"
    }
  ]
}
```

**`describe_table`** — Returns full column-level metadata for a given table, including types, nullability, constraints, and semantic descriptions.

**`get_metadata`** — Returns a complete metadata snapshot for the schema, useful for the agent to build a comprehensive understanding in a single call. Includes table relationships inferred from foreign keys.

### 5.3 Semantic Layer

Beyond raw database metadata, the server can serve an optional semantic layer that maps business concepts to tables and columns. This is stored in a YAML configuration per tenant:

```yaml
# semantic_layer/acme_corp.yml
entities:
  customer:
    table: customers
    primary_key: id
    description: "A person or organization that has purchased from us"
    columns:
      name:
        description: "Full legal name"
        pii: true
      lifetime_value:
        description: "Total revenue attributed to this customer in USD"
        aggregation: sum

  order:
    table: orders
    primary_key: id
    relationships:
      - column: customer_id
        references: customer.id
        type: many_to_one
```

The agent receives this alongside raw metadata, giving it both technical and business context.

---

## 6. Data Materialization

Materialization is the process of loading data from external sources into the tenant's schema and transforming it into a usable shape. This is a two-phase process: **load** (API ingestion) followed by **transform** (DBT).

### 6.1 Pipeline Registry

Available pipelines are defined in a pipeline registry. Each pipeline specifies its data sources, loading scripts, and DBT models:

```yaml
# pipelines/crm_sync.yml
pipeline: crm_sync
description: "Sync customer and deal data from CRM API"
version: "2.1"

sources:
  - name: customers
    loader: loaders/crm/customers.py
    schedule: "on_demand"
    api: crm_v2
    config:
      endpoint: "/api/v2/customers"
      pagination: cursor
      batch_size: 500

  - name: deals
    loader: loaders/crm/deals.py
    schedule: "on_demand"
    api: crm_v2
    config:
      endpoint: "/api/v2/deals"
      pagination: cursor

transforms:
  dbt_project: transforms/crm
  target_schema: "{{ schema_name }}"
  models:
    - stg_customers
    - stg_deals
    - fct_customer_deals
    - dim_customers
```

### 6.2 Materialization Flow

The `run_materialization` tool is a long-running operation. Rather than returning immediately and requiring the agent to poll for status, the server uses the MCP progress notification protocol to stream real-time updates back to the agent while the tool call remains open.

The agent host includes a `progressToken` in the tool call metadata:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {
    "name": "run_materialization",
    "arguments": { "pipeline": "crm_sync" },
    "_meta": {
      "progressToken": "mat-abc123"
    }
  }
}
```

The server then emits `notifications/progress` messages as the pipeline advances:

```
Agent calls run_materialization(pipeline="crm_sync")
       |
       v
+- Validate permissions (materialize:run scope) -----------+
|                                                           |
|  Resolve pipeline config from registry                    |
|  Calculate total steps (N sources + M DBT models)         |
|         |                                                 |
|         v                                                 |
|  Create materialization run record (state: started)       |
|         |                                                 |
|         v                                                 |
|  PHASE 1 -- LOAD                                          |
|  For each source in pipeline:                             |
|    1. Resolve API credentials from secrets store          |
|       (scoped to tenant)                                  |
|    2. Execute loader script in sandboxed subprocess       |
|    3. Write raw data to _raw_{source} staging tables      |
|    4. Emit progress notification ----------------------+  |
|         |                                              |  |
|         v                                              |  |
|  PHASE 2 -- TRANSFORM                                  |  |
|  1. Generate DBT profiles.yml targeting tenant schema  |  |
|  2. Run dbt run --select <models> --target <schema>    |  |
|  3. For each model completion:                         |  |
|     Emit progress notification ------------------------+  |
|  4. Capture DBT run results and logs                   |  |
|         |                                              |  |
|         v                                              |  |
|  Update schema metadata (materialized_at timestamps)   |  |
|  Return final tool response to agent                   |  |
+--------------------------------------------------------+  |
                                                            |
Progress notifications (streamed during execution): <-------+

  Step 1: {"progress": 1, "total": 6,
           "message": "Loading customers from CRM API..."}
  Step 2: {"progress": 2, "total": 6,
           "message": "Loaded 15,420 customer records"}
  Step 3: {"progress": 3, "total": 6,
           "message": "Loaded 8,731 deal records"}
  Step 4: {"progress": 4, "total": 6,
           "message": "Transform: stg_customers complete"}
  Step 5: {"progress": 5, "total": 6,
           "message": "Transform: stg_deals complete"}
  Step 6: {"progress": 6, "total": 6,
           "message": "Transform: fct_customer_deals complete"}
```

Each notification follows the MCP progress spec:

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/progress",
  "params": {
    "progressToken": "mat-abc123",
    "progress": 3,
    "total": 6,
    "message": "Loaded 8,731 deal records"
  }
}
```

The `progress` value increases monotonically as required by the spec. The `total` is calculated upfront as the sum of source loaders plus DBT models in the pipeline. The `message` field provides human-readable context that the agent can relay to the user.

Once all phases complete, the server returns the final tool response with a summary of the run. If the pipeline fails mid-execution, the server returns an error response with details about which step failed and what succeeded.

**Cancellation:** If the agent host sends an MCP `cancelled` notification for the in-flight request, the server terminates the active loader subprocess or DBT run gracefully and rolls back any partially loaded data.

**Fallback for disconnected clients:** The materialization run is always recorded in the `_catalog` with full phase-level status. If the MCP connection drops during a long run, the agent can reconnect and call `get_materialization_status` to retrieve the final result. This also supports agent hosts that don't provide a `progressToken` --- in that case, the tool blocks until completion and returns the full result without streaming updates.

### 6.3 Loader Execution

Loaders are Python scripts that follow a standard interface:

```python
# loaders/crm/customers.py
from dataexplorer.loaders import BaseLoader, LoadResult

class CustomerLoader(BaseLoader):
    """Loads customer records from the CRM API."""

    def load(self, context: LoadContext) -> LoadResult:
        client = context.get_api_client("crm_v2")
        schema = context.target_schema
        
        rows = []
        for page in client.paginate("/api/v2/customers"):
            rows.extend(page["data"])
        
        context.write_to_table(
            table=f"_raw_customers",
            data=rows,
            mode="replace"  # or "append" / "merge"
        )
        
        return LoadResult(rows_loaded=len(rows))
```

Loaders run in sandboxed subprocesses with limited network access (only to configured API endpoints). They receive a `LoadContext` that provides authenticated API clients (credentials resolved per-tenant from a secrets store) and database write access scoped to the target schema.

### 6.4 DBT Integration

The MCP server generates a DBT `profiles.yml` at runtime, targeting the tenant's schema:

```yaml
data_explorer:
  target: tenant_schema
  outputs:
    tenant_schema:
      type: postgres
      host: "{{ db_host }}"
      port: 5432
      user: "{{ service_user }}"
      password: "{{ service_password }}"
      dbname: "{{ database }}"
      schema: "acme_corp_user123_exploration"
      threads: 4
```

DBT models are versioned alongside the pipeline definitions. The server invokes DBT as a subprocess, capturing structured run results for reporting back to the agent.

### 6.5 Materialization Status (Fallback)

During normal operation, progress is streamed via MCP progress notifications (see 6.2). The `get_materialization_status` tool exists as a fallback for cases where the agent reconnects after a dropped connection or when the original tool call was made without a `progressToken`. It returns the same structured status stored in the `_catalog`:

```json
{
  "run_id": "mat_20260216_103000",
  "pipeline": "crm_sync",
  "state": "completed",
  "phases": {
    "load": {
      "state": "completed",
      "sources": {
        "customers": {"state": "loaded", "rows": 15420},
        "deals": {"state": "loaded", "rows": 8731}
      }
    },
    "transform": {
      "state": "completed",
      "models": {
        "stg_customers": "success",
        "stg_deals": "success",
        "fct_customer_deals": "success",
        "dim_customers": "success"
      }
    }
  },
  "started_at": "2026-02-16T10:30:00Z",
  "completed_at": "2026-02-16T10:32:14Z"
}
```

---

## 7. MCP Tool Definitions

The server exposes the following tools to the agent:

### 7.1 Metadata Tools

| Tool | Description | Required Scope |
|---|---|---|
| `list_databases` | List accessible databases for the current tenant | `data:read` |
| `list_tables` | List tables/views in the active schema with row counts and descriptions | `data:read` |
| `describe_table` | Get detailed column metadata, keys, and relationships for a table | `data:read` |
| `get_metadata` | Full schema metadata snapshot including semantic layer | `data:read` |

### 7.2 Query Tools

| Tool | Description | Required Scope |
|---|---|---|
| `query` | Execute a read-only SQL query against the active schema. Returns results as JSON with column metadata. Query timeout and row limit enforced server-side. | `data:read` |

### 7.3 Schema Tools

| Tool | Description | Required Scope |
|---|---|---|
| `provision_schema` | Create a new tenant-scoped schema (or return existing). Accepts optional `purpose` label. | `schema:provision` |
| `teardown_schema` | Drop a schema and all its contents. Requires confirmation parameter. | `schema:provision` |
| `list_schemas` | List all schemas owned by the current user within their tenant. | `data:read` |

### 7.4 Materialization Tools

| Tool | Description | Required Scope |
|---|---|---|
| `list_pipelines` | List available materialization pipelines and their descriptions | `materialize:run` |
| `run_materialization` | Trigger a pipeline run against the active schema. Streams progress via MCP `notifications/progress` if the caller provides a `progressToken`. Blocks until completion and returns a run summary. | `materialize:run` |
| `get_materialization_status` | Retrieve the status of a materialization run by ID. Primarily a fallback for reconnection scenarios — live progress is delivered via notifications. | `materialize:run` |
| `cancel_materialization` | Cancel a running materialization. Triggers graceful shutdown of active loader/DBT subprocesses. | `materialize:run` |

### 7.5 Tool Response Envelope

All tool responses follow a consistent envelope:

```json
{
  "success": true,
  "data": { ... },
  "tenant_id": "acme_corp",
  "schema": "acme_corp_user123_exploration",
  "warnings": [],
  "timing_ms": 142
}
```

On error:

```json
{
  "success": false,
  "error": {
    "code": "PERMISSION_DENIED",
    "message": "User lacks materialize:run scope",
    "detail": "Contact your admin to request the 'Data Engineer' role."
  }
}
```

---

## 8. Security Considerations

### 8.1 Query Safety

All agent-submitted SQL is executed with `SET ROLE` to the tenant role and restricted to the provisioned schema via `search_path`. Additional safeguards:

- **Statement timeout**: Queries are killed after a configurable timeout (default: 30s).
- **Row limit**: Results are capped at a maximum row count (default: 10,000) to prevent exfiltration of large datasets.
- **Read-only enforcement**: The tenant role is granted only `SELECT` on materialized tables. DDL and DML are not permitted through the `query` tool.
- **Query logging**: All queries are logged with the user ID, tenant ID, and execution time for audit purposes.

### 8.2 Secret Management

API credentials for data loaders are stored in a secrets store (e.g., AWS Secrets Manager, HashiCorp Vault) and are scoped to tenants. The MCP server retrieves secrets at loader execution time and never exposes them to the agent or in tool responses.

### 8.3 Network Isolation

Loader subprocesses run with network policies that restrict egress to only the configured API endpoints for the pipeline. The MCP server itself does not expose any API credentials to the agent layer.

### 8.4 Audit Trail

All MCP tool invocations are logged to an append-only audit table:

- User ID, tenant ID, session ID
- Tool name and arguments (with sensitive fields redacted)
- Response status and timing
- Schema context

---

## 9. Deployment

### 9.1 Transport Options

The MCP server supports two transport modes:

- **stdio** — For local development and testing. The agent host spawns the server as a child process.
- **SSE (Server-Sent Events)** — For production deployment. The server runs as an HTTP service behind a reverse proxy, with SSE for server-to-client streaming and POST for client-to-server messages. This is the mode that supports OAuth authentication flows.

### 9.2 Infrastructure

```
┌──────────┐     ┌───────────┐     ┌─────────────────┐
│  Agent   │────▶│  Gateway  │────▶│  MCP Server     │
│  Host    │ SSE │  (nginx)  │     │  (Python/Node)  │
└──────────┘     └───────────┘     └────────┬────────┘
                                            │
                    ┌───────────────────┬────┴─────────────┐
                    │                   │                   │
              ┌─────▼─────┐     ┌──────▼──────┐    ┌──────▼──────┐
              │ PostgreSQL │     │ Secrets Mgr │    │  Task Queue │
              │            │     │ (Vault/ASM) │    │  (Celery)   │
              └────────────┘     └─────────────┘    └─────────────┘
```

Materialization runs execute in background workers (e.g., Celery) while the MCP tool call remains open, streaming progress notifications back to the agent. The MCP server process subscribes to task events from the worker and translates them into MCP progress notifications. If the connection drops, the worker completes independently and the result is retrievable via `get_materialization_status`.

### 9.3 Scaling

- **MCP server instances** are stateless (session state in Redis or PostgreSQL). Horizontal scaling behind a load balancer with sticky sessions for SSE.
- **Database connection pooling** via PgBouncer or psycopg connection pool, with per-tenant pool limits.
- **Materialization workers** scale independently based on queue depth.

---

## 10. Design Decisions

1. **Schema reuse across sessions** — Users reuse existing schemas across sessions provided they haven't been removed due to inactivity (TTL expiry). The `provision_schema` tool returns an existing active schema if one matches the tenant/user/purpose key, refreshing its `last_accessed_at` timestamp. This avoids redundant materialization runs for returning users.

2. **Incremental materialization** — The initial implementation uses full-refresh pipelines exclusively. The loader interface and DBT model design should be structured to accommodate incremental loads (append-only, merge/upsert) in a future iteration, but this is out of scope for v1.

3. **Cross-tenant data** — A shared `_reference` schema for common reference data (country codes, currency rates, etc.) is architecturally supported but not required for v1. The schema isolation model already allows for a shared read-only schema if this becomes necessary.

4. **Agent-generated SQL writes** — v1 restricts the agent to read-only queries only. The tenant role is granted `SELECT` exclusively. If derived tables or materialized views become necessary in a future iteration, a `create_derived_table` tool wrapping `CREATE TABLE AS SELECT` with safety checks would be the preferred approach over granting raw DDL.

5. **Metadata persistence during cleanup** — Materialization pipelines may load additional metadata alongside the primary data tables. When a schema is torn down or data is cleared due to inactivity, this metadata must be preserved. The metadata is stored separately (in the `_catalog` schema or a dedicated `_metadata_{tenant_id}` schema) and is not affected by data schema lifecycle operations. This allows schema re-provisioning to skip metadata ingestion if it already exists for the tenant.

---

## 11. Open Questions

1. **MCP auth spec maturity** — The MCP authorization specification is still evolving. The pass-through token approach is more stable today, while the direct OAuth flow may need to adapt as the spec matures.

2. **Cost controls** — Materialization involves API calls and compute. Per-tenant quotas (max pipeline runs per day, max rows per load) should be configurable but the exact limits need discussion.
