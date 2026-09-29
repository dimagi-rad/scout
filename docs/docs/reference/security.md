# Security

How Scout limits what the agent and API callers can reach, and how it protects stored credentials.

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
multiple statements. Agent-authored queries must contain literal values rather
than parameter placeholders (`%s`, `?`, `:name`, or `$1`). Calls whose arguments
would be discarded during parsing or PostgreSQL generation are rejected.
Use `LIMIT` rather than `FETCH FIRST`; unsupported fetch
syntax is rejected instead of silently changing the requested row count.
Qualified table references must
use the workspace schema or `public`, without a database/catalog qualifier;
database role grants remain the access
boundary. System catalog references, including unqualified `pg_*` relations,
are rejected.

Raw SQL requires PostgreSQL 16 or later and supports an explicit allowlist of core PostgreSQL analytics functions:
aggregates, windows, dates, text, numeric operations, JSON, and arrays. Unknown,
custom, and extension function calls are rejected by name. This function-call
allowlist does not inspect the implementations of PostgreSQL operator overloads. Ordinary allowed calls are bound
to `pg_catalog` so tenant function overloads cannot change their resolution;
PostgreSQL special forms such as CASE, CAST, and COALESCE retain their syntax.
Functions that execute SQL passed as text are not supported. Casts are limited
to core PostgreSQL data types and arrays of those types; custom types and
OID-alias types such as `regclass` or `regnamespace` are rejected because their
input/output functions can resolve catalog objects. Expanding either allowlist
requires reviewing the function or type's behavior. Core JSONB/array operators
retain their PostgreSQL syntax, including `@@` for JSONB/JSONPath matching.
Full-text search types/functions and unlisted
members of otherwise supported function families are outside this allowlist;
use `ILIKE` or regular expressions for text search.

The server injects or caps the result limit and returns a `truncated` indicator.
The executed SQL and referenced tables, preserving explicit schema qualifiers,
are included with the result for provenance.

## Database isolation

### Read-only query roles

Each tenant schema has its own read-only PostgreSQL role. Both query paths run
under it:

- **Raw SQL** (MCP server): the pooled executor switches to the role, sets
  `search_path` to the workspace schema, enables
  `default_transaction_read_only`, and sets a 30-second `statement_timeout`.
  Because pooled connections use autocommit, each query runs in its own
  read-only transaction. `RESET ROLE` and `RESET ALL` run before the connection
  returns to the pool.
- **Semantic queries** (Cube): the database driver connects with the role,
  `search_path` set to the workspace schema and `public`, and a 30-second
  `statement_timeout` (`cube_config/cube.js`).

`search_path` only controls name resolution; the role's privileges are what
limit database access.

### Schema names

Scout mints schema and role names itself. A tenant schema name is the
sanitized external ID plus a digest of the provider and external ID, capped at
50 bytes (`apps/common/identifiers.py`), so two tenants can't map to the same
schema. Names are quoted with psycopg's `sql.Identifier` when they are put into
SQL.

### Connection pooling

The MCP server keeps a small set of connection pools to the managed database:
at most 4 pools of up to 10 connections each (`mcp_server/services/pool.py`).

## Encrypted credentials

API-key connection credentials (`TenantConnection.encrypted_credential`) are
encrypted at rest with Fernet. The key is the `DB_CREDENTIAL_KEY` environment
variable and is never stored in the database.

OAuth access and refresh tokens are stored by django-allauth in its
`SocialToken` table as plaintext; token refresh and data loading read them
directly. `EncryptingSocialAccountAdapter` encrypts them only in the copy
allauth serializes into the session during login.

## Rate limiting

- **Login**: 5 failed logins for an email within 5 minutes lock that email out
  of login. A successful login clears the counter. The Django admin
  login uses the same limiter.
- **Chat**: 20 messages per user per 60 seconds on `POST /api/chat/`. Over the
  limit the endpoint returns 429 with `Retry-After` and `X-RateLimit-*`
  headers. The `CHAT_RATE_LIMIT` and `CHAT_RATE_WINDOW` Django settings
  override the defaults.
- **DRF endpoints**: 60 requests per minute for anonymous clients and 120 per
  minute for signed-in users.

## Session security

- **Session cookies**: authentication uses HTTP-only session cookies (not JWT).
- **CSRF protection**: all mutating requests need a valid CSRF token. The SPA
  reads it from the CSRF cookie, which is not HTTP-only.
- **Allowed hosts**: `DJANGO_ALLOWED_HOSTS` restricts accepted host headers.
- **Trusted origins**: `CSRF_TRUSTED_ORIGINS` restricts which origins can make
  cross-origin requests.
- **Production**: secure cookies, HTTPS redirect and HSTS are enabled in
  `config/settings/production.py`.

## Unauthenticated routes

Scout has no public share links: no endpoint serves workspace content without
authentication. The unauthenticated routes are `/health/` (database and queue
status), `/widget.js`, the API landing page at `/`, and the allauth OAuth login
routes under `/accounts/`.

## MCP server security

The MCP server is the data access layer between the agent and the tenant
schemas.

- **Shared secret**: every request must carry an `X-Scout-MCP-Secret` header
  matching `MCP_SHARED_SECRET`, compared in constant time. If the secret is
  unset, the server rejects every request. Production settings refuse to start
  without it.
- **Server-side scope**: the agent graph injects `workspace_id`, `user_id` and
  `thread_id` into each call, and the model can't set them. The server resolves
  the workspace from them and checks the user's access.
- **Response envelopes**: every tool returns a consistent envelope with a
  structured error code (for example `AUTH_TOKEN_EXPIRED` or
  `WORKSPACE_ACCESS_DENIED`) and timing data.
- **Audit log**: each tool call writes a line to the `mcp_server.audit` logger
  with the tool, workspace, user, thread, status and duration.
- **Excluded tools**: `teardown_schema` is never given to the agent.

On the Django side, the MCP client opens a circuit breaker after five
consecutive connection failures and fails fast for 30 seconds
(`apps/agents/mcp_client.py`).
