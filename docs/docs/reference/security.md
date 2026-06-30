# Security

Scout implements multiple layers of security to protect data and prevent abuse.

## Semantic query access

Scout does not expose a raw SQL tool to the agent. User questions are answered
through semantic-model tools:

- `semantic_catalog` lists curated datasets and members.
- `describe_dataset` returns dataset details.
- `semantic_query` accepts structured measures, dimensions, filters, time
  dimensions, orderings, and limits.

Scout backend code compiles those structured requests into trusted,
parameterized database queries. Generated SQL is not part of the agent-facing
tool contract.

## Database isolation

### Encrypted credentials

Project database credentials (username and password) are encrypted at rest using Fernet symmetric encryption. The encryption key is stored in the `DB_CREDENTIAL_KEY` environment variable, never in the database.

### Read-only connections

Database connections use a read-only role (when configured) and set the PostgreSQL `search_path` to the project's configured schema, preventing access to other schemas.

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
