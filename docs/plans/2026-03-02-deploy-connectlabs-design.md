# Deploy Scout to Connect-Labs AWS Environment

## Goal

Deploy Scout to the same AWS environment running connect-labs so that
`labs.connect.dimagi.com` can load Scout in an iframe. No dedicated DNS
name — Scout is served under the `/scout/` path prefix on the existing
ALB.

## Architecture

### Compute — ECS Fargate on `labs-jj-cluster`

| ECS Service | Containers | CPU / Memory | Purpose |
|---|---|---|---|
| `labs-jj-scout-web` | `frontend` (nginx :3000) + `api` (uvicorn :8000) | 0.5 vCPU / 1 GB | React SPA + Django API |
| `labs-jj-scout-mcp` | `mcp-server` (:8100) | 0.25 vCPU / 512 MB | MCP data access layer |

Both containers in `labs-jj-scout-web` share `localhost` via awsvpc
networking, so nginx proxies to `localhost:8000`.

### Data

- **RDS**: Same PostgreSQL instance as connect-labs, new database
  `scout_labs`. Same RDS credentials, separate DB.
- **Redis**: Same ElastiCache endpoint as connect-labs, database number
  `/1` (connect-labs uses `/0`).

### Networking & Routing

ALB listener rules on the existing `labs.connect.dimagi.com` ALB:

| Priority | Condition | Target Group |
|---|---|---|
| 1 | Path = `/scout/*` | `labs-jj-scout-tg` (port 3000) |
| default | Path = `/*` | `labs-jj-web-tg` (connect-labs) |

### Request Flow

```
1. User visits labs.connect.dimagi.com/some-page
   → ALB default rule → connect-labs
   → Page renders <iframe src="https://labs.connect.dimagi.com/scout/embed/...">

2. Browser loads iframe: labs.connect.dimagi.com/scout/embed/...
   → ALB matches /scout/* → labs-jj-scout-tg
   → nginx strips /scout/ prefix
   → Serves SPA or proxies to Django at localhost:8000
```

Same-origin iframe avoids cross-origin cookie and CSP issues.

### Container Images — ECR

| Repository | Dockerfile | Contents |
|---|---|---|
| `labs-jj-scout` | `Dockerfile` | Python backend (Django + MCP server) |
| `labs-jj-scout-frontend` | `Dockerfile.frontend` | nginx + React SPA |

### Secrets

Stored in ECS task definition environment variables (set once via AWS
console). Same pattern as connect-labs.

## Path Prefix Handling (`/scout/`)

### ALB
Routes `/scout/*` to Scout target group. Does not strip prefix.

### nginx (`frontend/nginx.prod.conf`)
Receives `/scout/...`, strips prefix:
- `/scout/` → serves `index.html`
- `/scout/api/...` → proxy to `localhost:8000/api/...`
- `/scout/embed/...` → proxy to `localhost:8000/embed/...`
- `/scout/admin/...` → proxy to `localhost:8000/admin/...`
- `/scout/health/` → proxy to `localhost:8000/health/`
- `/scout/assets/...` → serve static frontend assets

### Vite (build time)
`base: '/scout/'` so all JS/CSS asset paths are prefixed correctly.
Driven by `VITE_BASE_PATH` env var, defaults to `/`.

### Django
Receives requests without `/scout/` prefix (nginx strips it).
`FORCE_SCRIPT_NAME=/scout` so Django generates correct absolute URLs
(e.g., OAuth redirects, CSRF referer checks).

## Django Settings: `config/settings/connectlabs.py`

Inherits from `production.py`. Overrides:

```python
FORCE_SCRIPT_NAME = env("FORCE_SCRIPT_NAME", default="/scout")
SECURE_SSL_REDIRECT = False  # ALB handles TLS termination
X_FRAME_OPTIONS = None  # Embed middleware handles this per-route
```

## ECS Task Definition Environment Variables

### `labs-jj-scout-web` — api container

```
DATABASE_URL=postgresql://USER:PASS@RDS_HOST:5432/scout_labs
DJANGO_SETTINGS_MODULE=config.settings.connectlabs
DJANGO_SECRET_KEY=<generated>
DB_CREDENTIAL_KEY=<generated-fernet-key>
ANTHROPIC_API_KEY=<key>
REDIS_URL=redis://ELASTICACHE_HOST:6379/1
MCP_SERVER_URL=http://scout-mcp.labs-local:8100/mcp
DJANGO_ALLOWED_HOSTS=labs.connect.dimagi.com
CSRF_TRUSTED_ORIGINS=https://labs.connect.dimagi.com
EMBED_ALLOWED_ORIGINS=https://labs.connect.dimagi.com
FORCE_SCRIPT_NAME=/scout
SECURE_SSL_REDIRECT=False
```

### `labs-jj-scout-mcp` — mcp-server container

```
DATABASE_URL=postgresql://USER:PASS@RDS_HOST:5432/scout_labs
DJANGO_SETTINGS_MODULE=config.settings.connectlabs
DJANGO_SECRET_KEY=<generated>
DB_CREDENTIAL_KEY=<generated-fernet-key>
```

## GitHub Actions Workflow

`.github/workflows/deploy-labs.yml` — modeled on connect-labs:

1. Manual trigger with optional `run_migrations` flag
2. OIDC AWS auth (same role as connect-labs)
3. Build & push backend image to ECR
4. Build & push frontend image to ECR
5. Optional: run migrations via one-off ECS task
6. Force new deployment on both services
7. Wait for stabilization

## One-Time AWS Setup (manual)

1. **ECR**: Create repos `labs-jj-scout` and `labs-jj-scout-frontend`
2. **RDS**: `CREATE DATABASE scout_labs;` on existing instance
3. **ECS Task Definitions**: Create for `labs-jj-scout-web` (2 containers)
   and `labs-jj-scout-mcp` (1 container) with env vars above
4. **ECS Services**: Create `labs-jj-scout-web` and `labs-jj-scout-mcp`
   on `labs-jj-cluster`
5. **Service Connect / CloudMap**: Register `scout-mcp.labs-local` so web
   task can reach MCP service
6. **ALB Target Group**: `labs-jj-scout-tg` pointing to scout-web port 3000
7. **ALB Listener Rule**: Path `/scout/*` → `labs-jj-scout-tg`
8. **Security Groups**: Allow ALB → Fargate on ports 3000/8100,
   Fargate → RDS on 5432, Fargate → ElastiCache on 6379

## Files Changed in Scout Repo

| File | Change |
|---|---|
| `config/settings/connectlabs.py` | New — connect-labs settings module |
| `frontend/nginx.prod.conf` | New — production nginx with /scout/ prefix stripping |
| `frontend/vite.config.ts` | Modified — read `VITE_BASE_PATH` env var |
| `Dockerfile.frontend` | Modified — accept build args for base path and nginx config |
| `.github/workflows/deploy-labs.yml` | New — GitHub Actions deploy workflow |
