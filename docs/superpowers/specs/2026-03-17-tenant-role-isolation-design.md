# PostgreSQL Role-Based Tenant Isolation for MCP Server Reads

**Date:** 2026-03-17
**Issue:** [#69 — Security: review database connection management in MCP server](https://github.com/dimagi-rad/scout/issues/69)
**Status:** Draft

## Problem

The MCP server connects to the managed database using a single privileged PostgreSQL user (from `MANAGED_DATABASE_URL`). Tenant isolation for user-facing queries relies on two application-level mechanisms:

1. `SET search_path TO {schema_name}` before each query (`query.py:51`)
2. SQL validation via `sqlglot` AST analysis — SELECT-only, schema allowlist, dangerous function blocking (`sql_validator.py`)

If either mechanism has a bug, the shared database user has access to every tenant's schema. The SQL validator is thorough (40+ blocked functions, statement type enforcement, schema allowlist), but it is the sole line of defence. A validator bypass or a new query path that skips validation would expose all tenant data.

## Goal

Add PostgreSQL-enforced isolation so that user-facing read queries execute under a role that can only access the target tenant's schema. The SQL validator becomes a second line of defence rather than the only one.

## Approach: `SET ROLE` per Query

Use a read-only PostgreSQL role per tenant schema. Before executing user-facing SQL, the MCP server issues `SET ROLE {role}`. PostgreSQL then enforces schema access regardless of what SQL is executed.

### Why `SET ROLE` over per-tenant connections

- Single connection pool, no per-tenant connection management
- The `MANAGED_DATABASE_URL` user already exists and has the needed privileges
- `SET ROLE` provides the same PostgreSQL-enforced boundary as separate users
- Forward-compatible with connection pooling (role switch + reset per query)

### Why not per-tenant PostgreSQL users

- Requires managing credentials per tenant (storage, rotation)
- Connection pooling becomes per-tenant, increasing resource usage
- The materializer needs broad DDL privileges anyway — a separate privileged connection would still exist

## Scope

### In scope

- `execute_query` in `query.py` — the only path that runs user/AI-generated SQL
- Role lifecycle in `SchemaManager` — creation on provision, deletion on teardown
- View schema roles for multi-tenant workspaces
- Backfill management command for existing schemas

### Out of scope

- `execute_internal_query` — runs trusted, parameterized SQL against `information_schema`; stays on the privileged role
- Materializer writes (`run_materialization`) — runs trusted DDL/DML, not user input
- `teardown_schema` — DDL operation, needs privileged role
- Connection pooling (separate concern, compatible with this design)

## Design

### Role naming

Deterministic derivation from schema name:

```python
def readonly_role_name(schema_name: str) -> str:
    return f"{schema_name}_ro"
```

Examples:
- Tenant schema `tenant_abc123` gets role `tenant_abc123_ro`
- View schema `ws_abc1234def567890` gets role `ws_abc1234def567890_ro`

No model changes. No migration. The role name is derived wherever needed.

### Role lifecycle — tenant schemas

In `SchemaManager.provision()` and `create_physical_schema()`, after `CREATE SCHEMA`:

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{schema_name}_ro') THEN
        CREATE ROLE {schema_name}_ro NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA {schema_name} TO {schema_name}_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema_name}
    GRANT SELECT ON TABLES TO {schema_name}_ro;
```

- `NOLOGIN` — the role cannot be used to connect directly; only via `SET ROLE`
- `GRANT USAGE` — allows the role to see the schema exists and access objects within it
- `ALTER DEFAULT PRIVILEGES` — tables created later by the materializer are automatically readable; no re-granting needed after each materialization run

In `SchemaManager.teardown()`, after `DROP SCHEMA ... CASCADE`:

```sql
DROP ROLE IF EXISTS {schema_name}_ro;
```

`DROP SCHEMA CASCADE` revokes all schema-level grants, so the role has no remaining privileges and can be cleanly dropped.

### Role lifecycle — view schemas (multi-tenant)

In `SchemaManager.build_view_schema()`, after creating the view schema and its views:

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ws_schema_name}_ro') THEN
        CREATE ROLE {ws_schema_name}_ro NOLOGIN;
    END IF;
END $$;

-- Grant access to the view schema itself
GRANT USAGE ON SCHEMA {ws_schema_name} TO {ws_schema_name}_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA {ws_schema_name}
    GRANT SELECT ON TABLES TO {ws_schema_name}_ro;

-- Grant read access to each constituent tenant schema (views reference them)
GRANT USAGE ON SCHEMA {tenant1_schema} TO {ws_schema_name}_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA {tenant1_schema} TO {ws_schema_name}_ro;
-- ... repeated for each tenant schema in the workspace
```

View schemas contain `UNION ALL` views that reference tables in the underlying tenant schemas. The view schema role needs `USAGE` + `SELECT` on each constituent tenant schema for the views to resolve.

In `SchemaManager.teardown_view_schema()`, after `DROP SCHEMA ... CASCADE`:

```sql
DROP ROLE IF EXISTS {ws_schema_name}_ro;
```

### Query execution changes

In `query.py`, `_execute_sync` changes from:

```python
cursor.execute(SET search_path TO {schema_name})
cursor.execute(SET statement_timeout TO '30s')
cursor.execute(sql)
```

To:

```python
cursor.execute(SET ROLE {readonly_role})
try:
    cursor.execute(SET search_path TO {schema_name})
    cursor.execute(SET statement_timeout TO '30s')
    cursor.execute(sql)
finally:
    cursor.execute(RESET ROLE)
```

`RESET ROLE` is in a `finally` block so it always executes, even on query errors. This prevents a failed query from leaving the connection in a restricted role state (important for future connection pooling).

**Only `execute_query` uses `SET ROLE`.** `execute_internal_query` continues to run on the privileged role because it queries `information_schema` with trusted, parameterized SQL.

### Context changes

`QueryContext` gets a derived property:

```python
@property
def readonly_role(self) -> str:
    return f"{self.schema_name}_ro"
```

No new constructor arguments. No model changes.

### Backfill for existing schemas

A Django management command `backfill_readonly_roles` that:

1. Queries all `TenantSchema` records with state in `[ACTIVE, MATERIALIZING]`
2. For each, creates the `_ro` role (idempotent — checks `pg_roles` first)
3. Grants `USAGE ON SCHEMA` and `SELECT ON ALL TABLES IN SCHEMA`
4. Sets `ALTER DEFAULT PRIVILEGES` for future tables
5. Repeats for all active `WorkspaceViewSchema` records (including constituent tenant schema grants)

The command is idempotent and safe to run multiple times.

### Error handling

If `SET ROLE` fails (e.g., role does not exist), the query fails with a clear error. The MCP server does **not** fall back to the privileged role. This is intentional — a missing role means the schema was provisioned without the role, which is a bug that should be surfaced, not silently ignored.

The error message returned to the user should be generic (e.g., "Schema configuration error — please contact an administrator") to avoid leaking internal details.

### Prerequisites

The `MANAGED_DATABASE_URL` PostgreSQL user must have the `CREATEROLE` privilege to create and drop the `_ro` roles. This is a one-time infrastructure change.

```sql
ALTER USER {managed_db_user} CREATEROLE;
```

## Deployment

1. Grant `CREATEROLE` to the managed database user
2. Deploy new code (schema manager creates roles for new schemas)
3. Run `python manage.py backfill_readonly_roles` (creates roles for existing schemas)

Order matters: step 2 before step 3 ensures no window where new schemas are created without roles. The backfill catches everything that existed before the deploy.

## Testing

### Unit tests

- `SET ROLE` to a tenant's read-only role, attempt `INSERT` — verify permission denied
- `SET ROLE` to tenant A's role, attempt `SELECT` from tenant B's schema — verify permission denied
- `SET ROLE` to tenant A's role, `SELECT` from tenant A's schema — verify success
- `RESET ROLE` executes even when the query raises an exception
- `SET ROLE` with a non-existent role — verify hard failure, no fallback

### Integration tests

- Provision a schema via `SchemaManager` — verify `_ro` role exists with correct grants
- Teardown a schema — verify role is dropped
- Build a view schema — verify view schema role can read through views to tenant data
- End-to-end: MCP `query` tool executes under the read-only role

### Backfill command tests

- Run against a schema with no role — verify role is created with correct grants
- Run against a schema with existing role — verify idempotent (no error)

## Files changed

| File | Change |
|------|--------|
| `apps/workspaces/services/schema_manager.py` | Add `readonly_role_name()` helper. Add role CREATE/GRANT in `provision()`, `create_physical_schema()`, `build_view_schema()`. Add role DROP in `teardown()`, `teardown_view_schema()`. |
| `mcp_server/services/query.py` | Wrap `_execute_sync` with `SET ROLE` / `RESET ROLE` for user-facing queries. Add `use_readonly_role` parameter to distinguish user vs internal execution paths. |
| `mcp_server/context.py` | Add `readonly_role` property to `QueryContext`. |
| `apps/workspaces/management/commands/backfill_readonly_roles.py` | New management command for existing schema backfill. |
| Tests | Role enforcement, lifecycle, and backfill tests. |

## What doesn't change

- `execute_internal_query` — trusted SQL, stays on privileged role
- Materializer — trusted writes, stays on privileged role
- SQL validator — stays as-is, becomes defence-in-depth
- No Django model migrations
- No changes to MCP tool interfaces or agent graph
