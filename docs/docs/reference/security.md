# Security

Scout implements multiple layers of security to protect data and prevent abuse.

## Semantic-first query access

The agent prefers the semantic model for analysis and must use `semantic_query`
for canonical metrics. `list_workspaces`, `list_datasets`, and
`describe_dataset` discover the available datasets and members. Scout compiles
structured semantic requests into trusted parameterized database queries.

The `query` tool is a read-only SQL fallback for questions the semantic model
cannot express, including inspecting raw text, exploring columns without
semantic members, and examining individual rows. The agent uses `list_tables`,
`describe_table`, and `get_metadata` to inspect the available data first. It must
explain why it used the fallback and distinguish its own calculations from
canonical metrics. `teardown_schema` is not exposed to the agent.

### Raw SQL validation

Before execution, the server parses SQL and requires a single SELECT, including
read-only CTEs, joins, and set operations. It rejects DML, DDL, `SELECT INTO`,
data-modifying CTEs, row-locking clauses, explicit `OPERATOR(...)` calls, and
multiple statements. Use `LIMIT` rather than `FETCH FIRST`; unsupported fetch
syntax is rejected instead of silently changing the requested row count.
Qualified table references must
use the workspace schema or `public`; database role grants remain the access
boundary. System catalog references, including unqualified `pg_*` relations,
are rejected.

Raw SQL supports an explicit allowlist of core PostgreSQL analytics functions:
aggregates, windows, dates, text, numeric operations, JSON, and arrays. Unknown,
custom, and extension functions are rejected. Ordinary allowed calls are bound
to `pg_catalog` so tenant function overloads cannot change their resolution;
PostgreSQL special forms such as CASE, CAST, and COALESCE retain their syntax.
Functions that execute SQL passed as text are not supported. Casts are limited
to core PostgreSQL data types and arrays of those types; custom types and
OID-alias types such as `regclass` or `regnamespace` are rejected because their
input/output functions can resolve catalog objects. Expanding either allowlist
requires reviewing the function or type's behavior.

The server injects or caps the result limit and returns a `truncated` indicator.
The executed SQL and referenced tables, preserving explicit schema qualifiers,
are included with the result for provenance.

## Database isolation

### Encrypted credentials

Project database credentials (username and password) are encrypted at rest using Fernet symmetric encryption. The encryption key is stored in the `DB_CREDENTIAL_KEY` environment variable, never in the database.

### Read-only connections

Both query paths run under the workspace's read-only database role. The pooled
executor sets `search_path` to the workspace schema and enables
`default_transaction_read_only` before execution. Because pooled connections
use autocommit, each query runs in a read-only transaction. Role and session
settings are reset before the connection returns to the pool. `search_path`
controls name resolution; PostgreSQL role privileges enforce database access.

### Statement timeout

Each connection sets a `statement_timeout` based on the project's `max_query_timeout_seconds` setting (default: 30 seconds). Long-running queries are automatically terminated.

### Connection pooling

Database connections are pooled per-project with a configurable maximum (`MAX_CONNECTIONS_PER_PROJECT`, default: 5).

## Rate limiting

### Login rate limiting

Login attempts are rate-limited per email address: 5 attempts within 5 minutes triggers a lockout. The counter resets on successful login.

### Query rate limiting

Semantic query execution is rate-limited per user at `MAX_QUERIES_PER_MINUTE` (default: 60 queries per minute).

## Session security

- **Session cookies** -- authentication uses HTTP-only session cookies (not JWT).
- **CSRF protection** -- all mutating requests require a valid CSRF token. The SPA reads the token from a non-HTTP-only CSRF cookie.
- **Allowed hosts** -- `DJANGO_ALLOWED_HOSTS` restricts which host headers are accepted.
- **Trusted origins** -- `CSRF_TRUSTED_ORIGINS` restricts which origins can make cross-origin requests.

## MCP server security

The MCP server acts as the data access layer between the agent and project databases. Security features include:

- **Auth token handling** -- OAuth tokens passed through to data sources are scrubbed from audit logs.
- **Error codes** -- Standardized error codes (e.g., `AUTH_TOKEN_EXPIRED`) allow the agent to respond appropriately to auth failures.
- **Response envelopes** -- All MCP responses use a consistent envelope format with timing data and audit metadata.
- **Circuit breaker** -- Repeated failures to a project database trigger a circuit breaker to prevent cascading timeouts.

## Schema name validation

Database schema names are validated with a regex pattern (`^[a-zA-Z_][a-zA-Z0-9_]*$`) to prevent SQL injection through schema names.
