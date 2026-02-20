# Data Explorer MCP Server — Design Document

**Author:** Simon  
**Date:** February 2026  
**Status:** Draft  

---

## 1. Overview

This document describes the design of an MCP (Model Context Protocol) server that provides a data exploration agent with secure, tenant-scoped access to databases. The server is responsible for:

- Accepting pass-through authentication from the Django agent host (session-based, no independent auth)
- Provisioning isolated database schemas scoped to the user's tenant (derived from OAuth provider claims)
- Materializing data into those schemas by orchestrating pre-defined data loading and transformation pipelines (API ingestion + DBT)
- Exposing rich database metadata so the agent can reason about available data

The MCP server acts as a controlled gateway: the agent never gets raw database credentials. Instead, it interacts through well-defined MCP tools that enforce tenant boundaries at every layer.

Data is materialized into a Scout-managed PostgreSQL instance (separate from Scout's application database). The storage backend is abstracted behind the Schema Manager to allow future migration to Snowflake or other analytical databases.

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
│  │ (passthru   │  │              │  │ (tenant ctx,   │  │
│  │  from host) │  │              │  │  schema refs)  │  │
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
     │       Scout-Managed Database (PostgreSQL)        │
     │                                                  │
     │  ┌──────────┐  ┌───────────────┐                  │
     │  │ dimagi   │  │ example_proj  │   ...            │
     │  │ (tenant) │  │ (tenant)      │                  │
     │  └──────────┘  └───────────────┘                  │
     └─────────────────────────────────────────────────┘
```

### 2.1 Component Summary

| Component | Responsibility |
|---|---|
| **Auth Layer** | Accepts pass-through tenant context from the Django agent host. The MCP server does not authenticate independently — it trusts the host to provide a validated user identity and tenant ID. |
| **Session Manager** | Maintains per-session state: tenant ID, provisioned schema name, materialization status. |
| **Schema Manager** | Creates and tears down tenant-scoped database schemas. Manages connection pools and schema-level access control. |
| **Metadata Service** | Introspects database catalogs and exposes table/column/relationship metadata to the agent. |
| **Materializer** | Orchestrates data loading from external APIs and runs DBT projects to transform raw data into analytics-ready tables. |
| **Tool Router** | Maps incoming MCP tool calls to the appropriate service, injecting the session's tenant context. |

---

## 3. Authentication

### 3.1 Auth Model — Pass-through from Django Host

The MCP server does not authenticate users independently. It runs behind the Django application, which handles all OAuth flows via django-allauth. The Django chat view validates the user's session, resolves their tenant context, and passes both the OAuth access token and tenant identity to the MCP server via request metadata.

```json
{
  "method": "tools/call",
  "params": {
    "name": "list_tables",
    "arguments": {},
    "_meta": {
      "tenant_id": "dimagi",
      "user_id": "user_123",
      "provider": "commcare",
      "oauth_tokens": {
        "commcare": "<access_token>"
      }
    }
  }
}
```

The MCP server trusts this metadata implicitly — it never validates tokens against an upstream IdP. Authentication and authorization are the responsibility of the Django layer.

All authenticated users have access to all MCP tools. There is no scope-based tool gating.

### 3.2 Tenant Resolution

Tenant identity is determined by the OAuth provider, not by token claims (the upstream providers are not OIDC-compliant). After a user authenticates via OAuth, the Django application resolves their tenant memberships by calling provider-specific APIs:

**CommCare HQ** — tenant is a "domain":

```
GET https://www.commcarehq.org/api/user_domains/v1/
Authorization: Bearer <access_token>

Response:
{
  "meta": {"limit": 20, "offset": 0, "total_count": 2},
  "objects": [
    {"domain_name": "dimagi", "project_name": "Dimagi"},
    {"domain_name": "example-project", "project_name": "Example Project"}
  ]
}
```

**CommCare Connect** — tenant is an "organization" (provider-specific API, similar pattern).

The user selects a domain/organization after login. This selection is stored as a `TenantMembership` in the Django database and persists across sessions:

```
TenantMembership
  user         → FK to User
  provider     → "commcare" | "commcare_connect"
  tenant_id    → "dimagi" (the domain_name / org identifier)
  tenant_name  → "Dimagi" (human-readable, from project_name)
  last_selected_at → timestamp (tracks which tenant was most recently active)
```

This model replaces the current `Project` + `ProjectMembership` pattern. It also provides a place to hang per-tenant configuration (available pipelines, materialization schedules, etc.) without reintroducing the project concept.

### 3.3 Session Tenant Context

The Django chat view resolves the tenant context before invoking the agent:

1. Look up the user's `TenantMembership` records.
2. Use the most recently selected tenant (or prompt the user to choose if multiple exist).
3. Retrieve the user's OAuth access token for the tenant's provider (encrypted at rest via Fernet, decrypted on read).
4. Pass `tenant_id`, `user_id`, `provider`, and `oauth_tokens` to the MCP server via `_meta`.

---

## 4. Schema Provisioning

### 4.1 Naming Convention

Each provisioned schema is namespaced to the tenant. All users within the same tenant share the schema:

```
{tenant_id}
```

For example: `dimagi` or `example_project`.

Schema lifecycle metadata is tracked via Django models in the application database (not in the managed database). This keeps all operational state in one place with standard Django migrations.

### 4.2 Implicit Provisioning

There is no explicit `provision_schema` tool. Schema creation is handled implicitly as the first step of a materialization run:

```
Agent calls run_materialization(pipeline="commcare_sync")
       │
       ▼
┌─ Validate tenant context (tenant_id from session metadata) ──┐
│                                                               │
│  Existing active schema for this tenant?                      │
│     │ yes                                                     │
│     ▼                                                         │
│  Refresh last_accessed_at, continue to materialization        │
│  (data from prior materializations preserved)                 │
│                                                               │
│     │ no                                                      │
│     ▼                                                         │
│  Step 1 of progress: "Provisioning schema for dimagi..."      │
│         │                                                     │
│         ▼                                                     │
│  CREATE SCHEMA with restricted GRANT                          │
│  - Schema owner: service role                                 │
│  - GRANT USAGE, SELECT to tenant role                         │
│  - SET search_path for session                                │
│         │                                                     │
│         ▼                                                     │
│  Register in Django TenantSchema model                        │
│         │                                                     │
│         ▼                                                     │
│  Continue to materialization (step 2+)                        │
└───────────────────────────────────────────────────────────────┘
```

If a metadata or query tool is called before any data has been materialized, it returns an informative error (e.g., "No data materialized yet for this domain. Would you like to run a pipeline?") that the agent can use to guide the user.

### 4.3 Lifecycle and Cleanup

Schemas have a configurable TTL (default: 24 hours from last access). A background job checks the `TenantSchema` model and drops expired schemas. Teardown is available as an MCP tool for manual cleanup.

**Important:** Schema teardown only removes the data schema itself (tables, views, and raw staging data). Metadata extracted during materialization (e.g., CommCare application structure, field definitions, semantic descriptions) is stored separately in Django models and persists across schema lifecycle events. This means a re-provisioned schema can skip metadata extraction if the tenant's metadata is already current.

Each `TenantSchema` tracks:

- `tenant` — FK to `TenantMembership`
- `schema_name` — the PostgreSQL schema name
- `created_at` — when it was provisioned
- `last_accessed_at` — updated on every query or materialization
- `state` — one of `provisioning`, `active`, `materializing`, `expired`, `teardown`
- `last_materialization` — FK to the most recent materialization run

### 4.4 Database-Level Isolation

The server uses PostgreSQL's role-based access control to enforce isolation. Each tenant gets a database role that can only access its own schema. The MCP server connects using a service role and uses `SET ROLE` to assume the tenant role for query execution, ensuring that even a SQL injection through the agent cannot cross tenant boundaries.

```sql
-- Service role creates the schema
CREATE SCHEMA dimagi;

-- Tenant role gets scoped access
GRANT USAGE ON SCHEMA dimagi TO role_dimagi;
GRANT SELECT ON ALL TABLES IN SCHEMA dimagi TO role_dimagi;

-- At query time
SET ROLE role_dimagi;
SET search_path TO dimagi;
```

---

## 5. Metadata Service

The metadata service gives the agent the context it needs to reason about available data without requiring it to write introspection queries directly.

### 5.1 Pipeline-Driven Metadata

Metadata is derived from the pipeline definitions and materialization records rather than live database introspection. Each pipeline defines the tables and columns it produces, and the materialization process records what was actually created. This is faster than querying `information_schema` on every request and avoids drift between what the pipeline claims and what the agent sees.

```
Pipeline Registry (static)
 └── Pipeline Definition
      └── Tables produced
           ├── Columns (name, type, description)
           └── Relationships

Materialization Records (dynamic, per-tenant)
 └── MaterializationRun
      └── Materialized tables
           ├── Row counts
           ├── Materialized_at timestamp
           └── Tenant-specific field metadata (from CommCare API)
```

### 5.2 Metadata Tools

**`list_tables`** — Returns all materialized tables in the tenant's schema, with row counts and descriptions from the pipeline + materialization records.

```json
{
  "tables": [
    {
      "name": "cases",
      "type": "table",
      "row_count": 15420,
      "description": "CommCare case records",
      "materialized_at": "2026-02-16T10:30:00Z",
      "pipeline": "commcare_sync"
    }
  ]
}
```

**`describe_table`** — Returns full column-level metadata for a given table, including types, nullability, and semantic descriptions. For CommCare data, includes tenant-specific field names and structure extracted during materialization.

**`get_metadata`** — Returns a complete metadata snapshot for the tenant's schema, useful for the agent to build a comprehensive understanding in a single call. Includes table relationships defined by the pipeline.

### 5.3 Tenant-Specific Semantic Metadata

CommCare is a platform where each tenant (domain) defines custom data structures — applications, forms, case types, and user-defined fields. The semantic layer cannot be static per-pipeline; it must be extracted per-tenant during materialization.

During the load phase of materialization, the loader queries the CommCare API to discover the tenant's data structure:

- **Application definitions** — which apps exist, their forms and modules
- **Case types** — the case types defined in the domain, with their properties
- **Form structure** — questions, field names, data types, choice lists
- **Custom field labels** — human-readable names for fields

This metadata is stored in Django models (not in the managed database) and persists across schema lifecycle events. When the agent calls `describe_table` or `get_metadata`, the response merges pipeline-defined table structure with tenant-specific field descriptions.

```json
{
  "name": "cases",
  "columns": [
    {"name": "case_id", "type": "text", "description": "Unique case identifier"},
    {"name": "case_type", "type": "text", "description": "Case type (e.g., 'patient', 'household')"},
    {"name": "owner_id", "type": "text", "description": "Mobile worker who owns this case"},
    {"name": "prop_age", "type": "integer", "description": "Patient age (custom property)"},
    {"name": "prop_village", "type": "text", "description": "Village name (custom property)"}
  ],
  "case_types": ["patient", "household", "referral"],
  "materialized_at": "2026-02-16T10:30:00Z"
}
```

This approach ensures the agent understands both the standard CommCare data model and the tenant's custom fields without requiring manual configuration.

---

## 6. Data Materialization

Materialization is the process of loading data from external sources into the tenant's schema and transforming it into a usable shape. This is a three-phase process: **discover** (extract tenant-specific metadata from the API), **load** (API ingestion), and **transform** (DBT).

### 6.1 Pipeline Registry

Available pipelines are defined in a pipeline registry. Each pipeline specifies its data sources, loading scripts, and DBT models:

```yaml
# pipelines/commcare_sync.yml
pipeline: commcare_sync
description: "Sync case and form data from CommCare HQ"
version: "1.0"
provider: commcare

sources:
  - name: cases
    loader: loaders/commcare/cases.py
    schedule: "on_demand"
    api: commcare_v2
    config:
      endpoint: "/a/{domain}/api/v0.5/case/"
      pagination: cursor
      batch_size: 500

  - name: forms
    loader: loaders/commcare/forms.py
    schedule: "on_demand"
    api: commcare_v2
    config:
      endpoint: "/a/{domain}/api/v0.5/form/"
      pagination: cursor

  - name: users
    loader: loaders/commcare/users.py
    schedule: "on_demand"
    api: commcare_v2
    config:
      endpoint: "/a/{domain}/api/v0.5/user/"

metadata_discovery:
  loader: loaders/commcare/metadata.py
  description: "Extract application structure, case types, and form definitions"

transforms:
  dbt_project: transforms/commcare
  target_schema: "{{ schema_name }}"
  models:
    - stg_cases
    - stg_forms
    - stg_users
    - dim_case_types
    - fct_form_submissions
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
    "arguments": { "pipeline": "commcare_sync" },
    "_meta": {
      "progressToken": "mat-abc123"
    }
  }
}
```

The server then emits `notifications/progress` messages as the pipeline advances:

```
Agent calls run_materialization(pipeline="commcare_sync")
       |
       v
+- Validate tenant context (tenant_id from session) -------+
|                                                           |
|  Resolve pipeline config from registry                    |
|  Auto-provision schema if needed (step 1 of progress)     |
|  Calculate total steps                                    |
|         |                                                 |
|         v                                                 |
|  Create materialization run record (state: started)       |
|         |                                                 |
|         v                                                 |
|  PHASE 1 -- DISCOVER                                      |
|  1. Query CommCare API for tenant metadata:               |
|     - Application definitions, case types, form structure |
|     - Custom field names and types                        |
|  2. Store metadata in Django models                       |
|  3. Emit progress notification ----------------------+    |
|         |                                            |    |
|         v                                            |    |
|  PHASE 2 -- LOAD                                     |    |
|  For each source in pipeline:                        |    |
|    1. Use OAuth token for CommCare API access         |    |
|    2. Execute loader (paginated REST API calls)       |    |
|    3. Write raw data to _raw_{source} staging tables  |    |
|    4. Emit progress notification --------------------+    |
|         |                                            |    |
|         v                                            |    |
|  PHASE 3 -- TRANSFORM                                |    |
|  1. Generate DBT profiles.yml targeting tenant schema |    |
|  2. Run dbt run --select <models> --target <schema>   |    |
|  3. For each model completion:                        |    |
|     Emit progress notification ----------------------+    |
|  4. Capture DBT run results and logs                  |    |
|         |                                             |    |
|         v                                             |    |
|  Update materialization record (timestamps, counts)   |    |
|  Return final tool response to agent                  |    |
+-------------------------------------------------------+   |
                                                             |
Progress notifications (streamed during execution): <--------+

  Step 1: {"progress": 1, "total": 8,
           "message": "Provisioning schema for dimagi..."}
  Step 2: {"progress": 2, "total": 8,
           "message": "Discovering tenant metadata from CommCare..."}
  Step 3: {"progress": 3, "total": 8,
           "message": "Loading cases from CommCare API..."}
  Step 4: {"progress": 4, "total": 8,
           "message": "Loaded 15,420 case records"}
  Step 5: {"progress": 5, "total": 8,
           "message": "Loaded 8,731 form submissions"}
  Step 6: {"progress": 6, "total": 8,
           "message": "Transform: stg_cases complete"}
  Step 7: {"progress": 7, "total": 8,
           "message": "Transform: stg_forms complete"}
  Step 8: {"progress": 8, "total": 8,
           "message": "Transform: fct_form_submissions complete"}
```

Each notification follows the MCP progress spec:

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/progress",
  "params": {
    "progressToken": "mat-abc123",
    "progress": 4,
    "total": 8,
    "message": "Loaded 15,420 case records"
  }
}
```

The `progress` value increases monotonically as required by the spec. The `total` is calculated upfront as the sum of discovery + source loaders + DBT models (plus schema provisioning if needed). The `message` field provides human-readable context that the agent can relay to the user.

Once all phases complete, the server returns the final tool response with a summary of the run. If the pipeline fails mid-execution, the server returns an error response with details about which step failed and what succeeded.

**Cancellation:** If the agent host sends an MCP `cancelled` notification for the in-flight request, the server terminates the active loader subprocess or DBT run gracefully and rolls back any partially loaded data.

**Fallback for disconnected clients:** The materialization run is always recorded in the Django database with full phase-level status. If the MCP connection drops during a long run, the agent can reconnect and call `get_materialization_status` to retrieve the final result. This also supports agent hosts that don't provide a `progressToken` — in that case, the tool blocks until completion and returns the full result without streaming updates.

### 6.3 Loader Execution

Loaders are Python scripts that follow a standard interface:

```python
# loaders/commcare/cases.py
from dataexplorer.loaders import BaseLoader, LoadResult

class CaseLoader(BaseLoader):
    """Loads case records from the CommCare HQ API."""

    def load(self, context: LoadContext) -> LoadResult:
        client = context.get_api_client("commcare_v2")
        domain = context.tenant_id

        rows = []
        for page in client.paginate(f"/a/{domain}/api/v0.5/case/"):
            rows.extend(page["objects"])

        context.write_to_table(
            table="_raw_cases",
            data=rows,
            mode="replace"  # or "append" / "merge"
        )

        return LoadResult(rows_loaded=len(rows))
```

Loaders run in sandboxed subprocesses with limited network access (only to configured API endpoints). They receive a `LoadContext` that provides authenticated API clients (using the user's OAuth access token, passed through from the Django session) and database write access scoped to the tenant's schema.

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
      schema: "dimagi"
      threads: 4
```

DBT models are versioned alongside the pipeline definitions. The server invokes DBT as a subprocess, capturing structured run results for reporting back to the agent.

### 6.5 Materialization Status (Fallback)

During normal operation, progress is streamed via MCP progress notifications (see 6.2). The `get_materialization_status` tool exists as a fallback for cases where the agent reconnects after a dropped connection or when the original tool call was made without a `progressToken`. It returns the same structured status stored in the Django `MaterializationRun` model:

```json
{
  "run_id": "mat_20260216_103000",
  "pipeline": "commcare_sync",
  "tenant_id": "dimagi",
  "state": "completed",
  "phases": {
    "discover": {
      "state": "completed",
      "case_types_found": 3,
      "applications_found": 2
    },
    "load": {
      "state": "completed",
      "sources": {
        "cases": {"state": "loaded", "rows": 15420},
        "forms": {"state": "loaded", "rows": 8731},
        "users": {"state": "loaded", "rows": 42}
      }
    },
    "transform": {
      "state": "completed",
      "models": {
        "stg_cases": "success",
        "stg_forms": "success",
        "stg_users": "success",
        "dim_case_types": "success",
        "fct_form_submissions": "success"
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

| Tool | Description |
|---|---|
| `list_tables` | List materialized tables in the tenant's schema with row counts, descriptions, and pipeline source |
| `describe_table` | Get detailed column metadata, keys, relationships, and tenant-specific field descriptions |
| `get_metadata` | Full metadata snapshot for the tenant's schema including all tables and semantic metadata |

Note: `list_databases` is removed — there is a single Scout-managed database with schema-level isolation.

### 7.2 Query Tools

| Tool | Description |
|---|---|
| `query` | Execute a read-only SQL query against the tenant's schema. Returns results as JSON with column metadata. Query timeout and row limit enforced server-side. |

### 7.3 Schema Tools

| Tool | Description |
|---|---|
| `teardown_schema` | Drop the tenant's schema and all its contents. Requires confirmation parameter. Schema will be re-provisioned on next materialization. |

Note: `provision_schema` and `list_schemas` are removed. Schema provisioning is implicit (happens as part of materialization). There is one schema per tenant.

### 7.4 Materialization Tools

| Tool | Description |
|---|---|
| `list_pipelines` | List available materialization pipelines and their descriptions |
| `run_materialization` | Trigger a pipeline run for the tenant. Auto-provisions schema if needed. Streams progress via MCP `notifications/progress` if the caller provides a `progressToken`. Blocks until completion and returns a run summary. |
| `get_materialization_status` | Retrieve the status of a materialization run by ID. Primarily a fallback for reconnection scenarios — live progress is delivered via notifications. |
| `cancel_materialization` | Cancel a running materialization. Triggers graceful shutdown of active loader/DBT subprocesses. |

All tools require an authenticated session with a valid tenant context. There is no per-tool scope gating — all authenticated users have access to all tools.

### 7.5 Tool Response Envelope

All tool responses follow a consistent envelope:

```json
{
  "success": true,
  "data": { ... },
  "tenant_id": "dimagi",
  "schema": "dimagi_user123_exploration",
  "warnings": [],
  "timing_ms": 142
}
```

On error:

```json
{
  "success": false,
  "error": {
    "code": "TENANT_NOT_FOUND",
    "message": "No active tenant context for this session",
    "detail": "Select a domain before querying data."
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

API credentials for data loaders come from the user's OAuth access tokens, which are stored encrypted at rest (Fernet) in the Django database via django-allauth's `SocialToken` model. The Django chat view decrypts tokens and passes them to the MCP server via `_meta`. The MCP server never persists tokens and never exposes them to the agent or in tool responses. Token refresh is handled by the Django layer using the stored refresh token before passing the access token downstream.

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

1. **Pass-through auth only** — The MCP server does not authenticate users independently. It trusts the Django host to validate sessions, resolve tenant context, and pass OAuth tokens. This avoids duplicating auth logic and aligns with the current architecture where the MCP server runs behind Django. If the MCP server is ever exposed directly to external agent hosts, this decision should be revisited.

2. **Tenant context from OAuth providers, not projects** — The `Project` + `ProjectMembership` model is replaced by `TenantMembership`, where tenant identity comes from the OAuth provider (CommCare domain, CommCare Connect organization). This eliminates the project selection step and ties data access directly to the user's organizational identity.

3. **No scope-based tool gating** — All authenticated users have access to all MCP tools. Per-tool permissions add complexity without clear benefit at this stage. If role-based restrictions become necessary, they can be added at the Django layer before invoking the agent.

4. **Scout-managed database** — Data is materialized into a Scout-managed PostgreSQL instance, separate from Scout's application database. The storage backend is abstracted behind the Schema Manager to allow future migration to Snowflake or other analytical databases. Users never provide database connection strings.

5. **Shared per-tenant schemas** — All users within the same tenant (CommCare domain) share a single schema. This avoids duplicating materialized data across users who see the same underlying CommCare data. If multi-provider schemas become necessary (e.g., a user wants CommCare + CommCare Connect data in one schema), per-user schemas can be introduced later.

6. **Implicit schema provisioning** — There is no explicit `provision_schema` tool. The schema is auto-created as the first step of a materialization run, with progress reported to the user. Query/metadata tools return an informative error if called before any data has been materialized.

7. **Pipeline-driven metadata** — Metadata is derived from pipeline definitions and materialization records rather than live DB introspection. This is faster and avoids drift. Tenant-specific semantic metadata (custom CommCare fields, case types, form structure) is extracted from the CommCare API during the "discover" phase of materialization and stored in Django models.

8. **Schema catalog as Django models** — Schema lifecycle, materialization runs, and tenant metadata are tracked via Django ORM models in the application database (not in the managed database). This keeps all operational state together with standard migrations.

9. **Schema reuse across sessions** — Tenant schemas persist across user sessions, refreshing `last_accessed_at` on each access. This avoids redundant materialization runs for returning users.

6. **Incremental materialization** — The initial implementation uses full-refresh pipelines exclusively. The loader interface and DBT model design should be structured to accommodate incremental loads (append-only, merge/upsert) in a future iteration, but this is out of scope for v1.

7. **Cross-tenant data** — A shared `_reference` schema for common reference data (country codes, currency rates, etc.) is architecturally supported but not required for v1. The schema isolation model already allows for a shared read-only schema if this becomes necessary.

8. **Agent-generated SQL writes** — v1 restricts the agent to read-only queries only. The tenant role is granted `SELECT` exclusively. If derived tables or materialized views become necessary in a future iteration, a `create_derived_table` tool wrapping `CREATE TABLE AS SELECT` with safety checks would be the preferred approach over granting raw DDL.

10. **Metadata persistence during cleanup** — Materialization pipelines extract tenant-specific metadata (CommCare application structure, case types, custom fields) during the discover phase. When a schema is torn down due to inactivity, this metadata persists in Django models and is not affected by schema lifecycle operations. This allows a re-provisioned schema to skip the discover phase if the tenant's metadata is already current.

---

## 11. Open Questions

1. **CommCare Connect tenant resolution** — The API for resolving a CommCare Connect user's organization memberships needs to be identified (equivalent to CommCare HQ's `/api/user_domains/v1/`).

2. **Cost controls** — Materialization involves API calls and compute. Per-tenant quotas (max pipeline runs per day, max rows per load) should be configurable but the exact limits need discussion.

3. **Token refresh during long materializations** — OAuth access tokens may expire during a long-running materialization pipeline. The Django layer should refresh tokens proactively (the `token_refresh` service exists but is not yet wired into the chat flow). The MCP server needs a way to request a refreshed token mid-pipeline, or the Django layer should ensure tokens have sufficient remaining lifetime before starting a materialization.

4. **Multi-domain users** — A CommCare user may belong to multiple domains. The UI needs a domain selector, and the session must track which domain is active. Switching domains mid-conversation may require re-provisioning a schema.

5. **Multi-provider data** — Users may want to combine data from CommCare HQ and CommCare Connect in a single analysis. The current shared-per-tenant model assumes one provider per schema. If cross-provider analysis is needed, per-user schemas or a cross-tenant join mechanism would be required.
