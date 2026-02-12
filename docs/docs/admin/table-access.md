# Table access

Scout provides fine-grained control over which database tables the agent can see and query. This is configured per-project.

## How it works

Each project has two JSON fields that control table visibility:

- **Allowed tables** -- a list of table names the agent can access.
- **Excluded tables** -- a list of table names to hide from the agent.

### Resolution rules

1. If **allowed tables** is empty (the default), all tables in the project's schema are visible.
2. If **allowed tables** is populated, only those tables are visible.
3. **Excluded tables** are always hidden, regardless of the allowed list.

In other words: allowed tables is an allowlist, excluded tables is a blocklist, and the blocklist wins.

## Schema isolation

Each project is scoped to a single PostgreSQL schema (default: `public`). The agent can only see tables within that schema. The connection's `search_path` is set to the project's configured schema, preventing access to other schemas.

Schema names are validated to prevent SQL injection -- they must match the pattern `^[a-zA-Z_][a-zA-Z0-9_]*$`.

## Configuration

Configure table access in the project edit form under the **Access Control** section. Navigate to **Projects** in the sidebar, click a project, and expand **Access Control**.

### Allow only specific tables

Set **Allowed tables** to a JSON list:

```json
["orders", "customers", "products", "order_items"]
```

The agent will only see these four tables, even if the schema contains dozens more.

### Exclude sensitive tables

Set **Excluded tables** to a JSON list:

```json
["user_credentials", "api_keys", "audit_log"]
```

These tables are hidden from the agent regardless of the allowed tables setting.

### Combine both

You can use both fields together. For example, to allow all tables except a few sensitive ones, leave **Allowed tables** empty and set **Excluded tables**:

```json
["user_credentials", "api_keys"]
```

Or to allow a specific set but exclude one of them for testing:

```json
// Allowed tables
["orders", "customers", "products", "staging_orders"]

// Excluded tables
["staging_orders"]
```

## SQL enforcement

Table access controls are enforced at the SQL validation layer. The `SQLValidator` parses queries using sqlglot and checks that every referenced table is allowed. If a query references a disallowed table, it is rejected before execution.
