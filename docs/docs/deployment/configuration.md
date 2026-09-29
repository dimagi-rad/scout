# Configuration

Scout is configured via environment variables, typically set in a `.env` file in the project root.

## Required variables

| Variable | Description |
|----------|-------------|
| `DJANGO_SETTINGS_MODULE` | Settings module: `config.settings.development`, `config.settings.production`, `config.settings.connectlabs` (production settings served under the `/scout` path prefix), or `config.settings.test`. There is no default; `manage.py`, the ASGI/WSGI apps and the MCP server refuse to start without it. `DEBUG` is fixed by the module (on in development, off otherwise). |
| `DJANGO_SECRET_KEY` | Django secret key for cryptographic signing. Must be unique and secret in production. |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude. The agent can't run without it. |
| `DB_CREDENTIAL_KEY` | Fernet key for encrypting API-key credentials at rest. OAuth tokens are stored by allauth without this encryption; the key only covers the copy kept in the session during login. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DATABASE_URL` | PostgreSQL connection URL for Scout's own database. Example: `postgresql://user:pass@localhost/scout` |
| `CUBE_API_URL` | Cube semantic query API. Host development: `http://localhost:4000`; use the private service URL in production. |
| `CUBE_VALIDATOR_URL` | Cube schema validator. Host development: `http://localhost:4010`; use the private service URL in production. |
| `CUBEJS_API_SECRET` | Shared signing secret for Scout and Cube. Both must use the same value. |
| `MANAGED_DATABASE_URL` | PostgreSQL connection URL for materialized data. Required outside development; see [Managed database](#managed-database). |
| `MCP_SHARED_SECRET` | Required by the production settings, which refuse to start without it. See [MCP server](#mcp-server). |

## Optional variables

### Django

| Variable | Default | Description |
|----------|---------|-------------|
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated list of allowed host headers. |
| `DEPLOY_ENVIRONMENT` | `production` for the production or connectlabs settings, otherwise `development` | Environment label used by Sentry, Task Badger and the CommCare Connect host. Set `staging` for a staging deployment that uses the production settings. |
| `SCOUT_BASE_URL` | `http://localhost:5173` | Public URL of the Scout frontend, used for links in emails sent from the background worker. |

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `CSRF_TRUSTED_ORIGINS` | `http://localhost:5173` | Comma-separated list of trusted origins for CSRF. Set to your frontend's URL in production. The development settings replace it with a fixed list of localhost origins. |
| `EMBED_ALLOWED_ORIGINS` | (empty) | Comma-separated list of origins allowed to embed Scout's `/embed/` route in an iframe (e.g. `https://labs.example.com`). The development settings replace it with a fixed list of localhost origins. See [Embedding Scout](#embedding-scout) below. |

### Authentication

OAuth client IDs and secrets live in allauth social application records. `manage.py setup_oauth_apps` creates or updates them from `COMMCARE_OAUTH_*`, `CONNECT_OAUTH_*`, `OCS_OAUTH_*`, `GOOGLE_OAUTH_*` and `GITHUB_OAUTH_*` `CLIENT_ID`/`CLIENT_SECRET` pairs. When `DEPLOY_ENVIRONMENT=staging` it reads Connect's from `STAGING_CONNECT_OAUTH_*` instead. You can also manage them in Django admin.

| Variable | Default | Description |
|----------|---------|-------------|
| `SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS` | `{"commcare": ["dimagi.com"]}` | JSON map of OAuth provider ID to allowed email domains. Providers not listed are unrestricted. A provider with a list rejects other domains and logins with no email. |
| `ACCOUNT_DEFAULT_HTTP_PROTOCOL` | `http` | Protocol allauth uses for OAuth callback URLs. Set `https` behind TLS. |
| `CONNECT_API_URL` | `https://connect-staging.dimagi.com` when `DEPLOY_ENVIRONMENT=staging`, otherwise `https://connect.dimagi.com` | CommCare Connect API and OAuth host. |
| `OCS_URL` | `https://www.openchatstudio.com` | Open Chat Studio API and OAuth host. |

### Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | (empty) | Shared Redis cache URL. Set this for multi-worker/multi-process deployments so login lockouts and rate limits share state. Without it, caches and limits are per-process and reset on restart. Background jobs use PostgreSQL, not Redis. |

### MCP server

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_SERVER_URL` | `http://localhost:8100/mcp` | URL of the MCP server the agent uses for data access. |
| `MCP_SHARED_SECRET` | (empty; the development settings use a fixed local value) | Secret the API and worker send to the MCP server, which rejects requests without it. Set the same value on all three. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

### Managed database

The managed database stores materialized data. Each tenant (a CommCare domain, Connect opportunity or Open Chat Studio chatbot) gets its own PostgreSQL schema.

| Variable | Default | Description |
|----------|---------|-------------|
| `MANAGED_DATABASE_URL` | (empty) | PostgreSQL connection URL for materialized data. Only the development settings fill it in when it is unset, pointing it at the main app database; tenant schemas still keep the data separate. |

### Access control

These are staged-rollout switches.

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT` | `False` | When on, a member can use a workspace only if their own credentials cover every one of its data sources; when off, any one is enough. Run `manage.py report_workspace_credential_coverage` before turning it on. Production runs with it on (set in `config/deploy.yml`, `config/deploy-worker.yml` and `config/deploy-mcp.yml`); each coverage denial the gate makes is logged at INFO as `workspace_access_denied_coverage` with the user, workspace, tenant ids and gap codes, at most once per user and workspace per request. The line also appears on requests a remediation action then lets through (leaving, removing the missing source), since the member is still not covered. To roll back, set it to `"False"` in all three and redeploy. |
| `UPSTREAM_ACCESS_FRESHNESS_ENFORCED` | `False` | When on, workspace access also requires the user's access to have been confirmed with the upstream provider recently. Enable it in the API, worker and MCP server together. |

### LLM and agent

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_LLM_MODEL` | `claude-opus-4-8` | Claude model the agent uses. |
| `LANGGRAPH_CHECKPOINT_POOL_MIN_SIZE` | `1` (`0` in development) | Minimum size of each process's connection pool for conversation checkpoints. |
| `LANGGRAPH_CHECKPOINT_POOL_MAX_SIZE` | `20` (`4` in development) | Maximum size of that pool. The pool is per process, so multiply by the number of API and worker processes when sizing PostgreSQL `max_connections`. |
| `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_BASE_URL` | (empty) | Langfuse tracing for agent runs. Leave blank to disable. |

### Email

The development settings always print email to the console. Elsewhere the base settings default to the console backend and the production settings to Amazon SES.

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL_BACKEND` | Console (base settings); `anymail.backends.amazon_ses.EmailBackend` (production settings) | Django email backend. |
| `AWS_SES_REGION` | `us-east-1` | SES region, used by the production settings. |
| `AWS_SES_ACCESS_KEY_ID`, `AWS_SES_SECRET_ACCESS_KEY` | (empty) | Explicit SES credentials. When unset, boto3's default credential chain is used. |
| `DEFAULT_FROM_EMAIL` | `Scout <noreply@scout.dimagi.com>` | Sender address. |

### Error monitoring (Sentry)

Sentry is off by default. Setting `SENTRY_DSN` activates it for the API, Procrastinate worker, and MCP server (they all load Django settings). For the frontend, source maps can optionally be uploaded at build time so minified stack traces resolve back to TypeScript source.

| Variable | Default | Description |
|----------|---------|-------------|
| `SENTRY_DSN` | (empty) | Backend Sentry DSN. Leave blank to disable. |
| `SENTRY_ENVIRONMENT` | `DEPLOY_ENVIRONMENT` | Event environment tag. |
| `SENTRY_RELEASE` | (empty) | Release identifier; set to a git SHA or version string to match stack frames to builds. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | Fraction of transactions to trace (0–1). `0.0` means errors only. |
| `SENTRY_SEND_DEFAULT_PII` | `False` | Whether sentry-sdk captures request headers, user info, etc. |
| `SENTRY_SUPPRESS_EXPECTED_STATES` | `True` | Drop errors that represent known, routine operational states rather than defects before they reach Sentry. |
| `VITE_SENTRY_DSN` | (empty) | Frontend DSN. Baked into the bundle at build time; must be set at `bun run build` (not runtime). |
| `VITE_SENTRY_ENVIRONMENT` | Vite's `MODE` | Environment tag for browser events. |
| `VITE_SENTRY_RELEASE` | (empty) | Release tag for browser events. Should match `SENTRY_RELEASE` server-side. |
| `VITE_SENTRY_TRACES_SAMPLE_RATE` | `0` | Browser performance sampling. |

For frontend source map upload (recommended — otherwise stack traces show minified code): set `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` in the build environment. All three must be set for the Vite plugin to activate. `@sentry/vite-plugin` generates hidden source maps, uploads them, then deletes them so they don't ship to browsers.

### Background task tracking

| Variable | Default | Description |
|----------|---------|-------------|
| `TASKBADGER_API_KEY` | (empty) | Task Badger project API key for tracking background jobs. Leave blank to disable. |
| `TASKBADGER_ENVIRONMENT` | `DEPLOY_ENVIRONMENT` | Environment tag for Task Badger. |

## Frontend environment

The frontend uses Vite and proxies API requests to the backend in development. No frontend-specific environment variables are required for development. For production builds, the frontend is served as static files and API requests are routed by the reverse proxy.

## Embedding Scout

Scout exposes an `/embed/` route that renders a trimmed-down SPA shell designed for cross-origin iframe embedding. Any site can embed Scout by dropping in the widget script and pointing it at a Scout deployment:

```html
<script src="https://scout.example.com/widget.js"></script>
<div id="scout-container" style="height: 100vh;"></div>
<script>
  ScoutWidget.init({
    container: "#scout-container",
    mode: "full",
    theme: "light",
    tenant: "<opportunity-or-tenant-id>",
  });
</script>
```

To allow your host site to embed Scout, set `EMBED_ALLOWED_ORIGINS` to a comma-separated list of host origins on **both** the API and frontend containers (e.g. `https://host.example.com,https://staging.example.com`). The frontend container renders the list into the `/embed/` route's `Content-Security-Policy: frame-ancestors` header at startup; the API container switches session + CSRF cookies to `SameSite=None` so they flow on iframe requests.

With `EMBED_ALLOWED_ORIGINS` unset, `/embed/` collapses to same-origin-only framing — a safe default for deployments that don't need cross-origin embedding.
