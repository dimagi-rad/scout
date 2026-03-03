# Deploy Scout to Connect-Labs Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy Scout to the connect-labs AWS environment so it can be loaded as an iframe from `labs.connect.dimagi.com` under the `/scout/` path prefix.

**Architecture:** ECS Fargate services on existing `labs-jj-cluster`, sharing RDS (new `scout_labs` database) and Redis (database `/1`). ALB path-based routing sends `/scout/*` to Scout. Nginx strips the prefix before proxying to Django.

**Tech Stack:** Django, React/Vite, nginx, AWS ECS Fargate, ECR, ALB, GitHub Actions

---

### Task 1: Create connect-labs Django settings module

**Files:**
- Create: `config/settings/connectlabs.py`

**Step 1: Create the settings file**

```python
"""
Django settings for Scout deployed to the connect-labs AWS environment.

Inherits production security settings but configures for:
- ALB TLS termination (no SSL redirect)
- /scout/ path prefix (FORCE_SCRIPT_NAME)
- iframe embedding from labs.connect.dimagi.com
"""

import environ

env = environ.Env()

from .production import *  # noqa: F401, F403

# ALB terminates TLS, so don't redirect HTTP → HTTPS at Django level
SECURE_SSL_REDIRECT = False

# Scout is served under /scout/ path prefix on the ALB
FORCE_SCRIPT_NAME = env("FORCE_SCRIPT_NAME", default="/scout")

# Allow iframe embedding — the EmbedFrameOptionsMiddleware handles
# per-route CSP headers, so remove the blanket DENY.
X_FRAME_OPTIONS = None
```

**Step 2: Verify the settings chain loads correctly**

Run: `DJANGO_SETTINGS_MODULE=config.settings.connectlabs DJANGO_SECRET_KEY=test python -c "from django.conf import settings; print(settings.FORCE_SCRIPT_NAME, settings.SECURE_SSL_REDIRECT, settings.DEBUG)"`

Expected: `/scout False False`

**Step 3: Commit**

```bash
git add config/settings/connectlabs.py
git commit -m "feat: add connect-labs Django settings module"
```

---

### Task 2: Add base path support to the frontend API client

The SPA makes fetch calls to absolute paths like `/api/auth/providers/`.
Under `/scout/`, these must become `/scout/api/auth/providers/` so the
ALB routes them to Scout instead of connect-labs.

**Files:**
- Create: `frontend/src/config.ts`
- Modify: `frontend/src/api/client.ts`

**Step 1: Create the frontend config module**

```typescript
// frontend/src/config.ts

/**
 * Runtime base path for the app, derived from the Vite build-time env var.
 * Defaults to "" (root) for local development.
 *
 * Examples:
 *   local dev:    ""
 *   connect-labs: "/scout"
 */
export const BASE_PATH = (import.meta.env.VITE_BASE_PATH || "").replace(/\/$/, "")
```

**Step 2: Update the API client to prepend BASE_PATH**

In `frontend/src/api/client.ts`, add the import and prefix all fetch URLs:

```typescript
// Add at top of file:
import { BASE_PATH } from "@/config"

// In the request() function, prefix the URL:
async function request<T>(
  url: string,
  options: RequestInit & { rawBody?: boolean } = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase()
  const { rawBody, ...fetchOptions } = options

  const headers: Record<string, string> = {
    ...(rawBody ? {} : { "Content-Type": "application/json" }),
    ...(activeCustomWorkspaceId && { "X-Custom-Workspace": activeCustomWorkspaceId }),
    ...(fetchOptions.headers as Record<string, string> | undefined),
  }

  if (method !== "GET" && method !== "HEAD") {
    headers["X-CSRFToken"] = getCsrfToken()
  }

  const res = await fetch(`${BASE_PATH}${url}`, {
    ...fetchOptions,
    headers,
    credentials: "include",
  })
  // ... rest unchanged
```

Also update `getBlob`:

```typescript
  getBlob: async (url: string): Promise<Blob> => {
    const res = await fetch(`${BASE_PATH}${url}`, { credentials: "include" })
```

**Step 3: Update OAuth login links in LoginForm**

In `frontend/src/components/LoginForm/LoginForm.tsx`, the OAuth `?next=` redirect
must also use the base path:

```typescript
import { BASE_PATH } from "@/config"

// In the provider button:
<a href={`${provider.login_url}?next=${BASE_PATH}/`}>
```

Note: `provider.login_url` comes from Django which already includes `FORCE_SCRIPT_NAME`,
so it will be `/scout/accounts/commcare/login/` in connect-labs. The `next` param
needs the base path so Django redirects back to `/scout/` after OAuth.

**Step 4: Verify locally (no base path set = no change)**

Run: `cd frontend && bun dev`

Confirm the app works normally with no `VITE_BASE_PATH` set (all URLs remain `/api/...`).

**Step 5: Commit**

```bash
git add frontend/src/config.ts frontend/src/api/client.ts frontend/src/components/LoginForm/LoginForm.tsx
git commit -m "feat: add BASE_PATH support to API client and OAuth links"
```

---

### Task 3: Add base path support to React routers

Both `createBrowserRouter` calls need a `basename` so client-side routing
works under `/scout/`.

**Files:**
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/pages/EmbedPage.tsx`

**Step 1: Update the main router**

```typescript
// frontend/src/router.tsx
import { BASE_PATH } from "@/config"

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      // ... unchanged
    ],
  },
], { basename: BASE_PATH || undefined })
```

**Step 2: Update the embed router**

```typescript
// frontend/src/pages/EmbedPage.tsx
import { BASE_PATH } from "@/config"

const embedRouter = createBrowserRouter([
  {
    path: "/embed",
    element: <EmbedLayout />,
    children: [
      // ... unchanged
    ],
  },
], { basename: BASE_PATH || undefined })
```

**Step 3: Verify locally**

Run: `cd frontend && bun dev`

Confirm client-side routing still works with no `VITE_BASE_PATH` set.

**Step 4: Commit**

```bash
git add frontend/src/router.tsx frontend/src/pages/EmbedPage.tsx
git commit -m "feat: add basename to React routers for path prefix support"
```

---

### Task 4: Add VITE_BASE_PATH to Vite config

Vite's `base` option controls the public path for all assets (JS, CSS, images).

**Files:**
- Modify: `frontend/vite.config.ts`

**Step 1: Update vite.config.ts**

```typescript
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "")

  return {
    base: env.VITE_BASE_PATH || "/",
    plugins: [react(), tailwindcss()],
    // ... rest unchanged
  }
})
```

**Step 2: Test build with base path**

Run: `cd frontend && VITE_BASE_PATH=/scout/ bun run build`

Verify that `dist/index.html` references assets as `/scout/assets/...`.

**Step 3: Test build without base path (default)**

Run: `cd frontend && bun run build`

Verify that `dist/index.html` references assets as `/assets/...`.

**Step 4: Commit**

```bash
git add frontend/vite.config.ts
git commit -m "feat: add VITE_BASE_PATH to Vite config for asset prefix"
```

---

### Task 5: Create production nginx config with path prefix stripping

This nginx config handles the `/scout/` prefix from the ALB and proxies
to Django on `localhost:8000` (same ECS task, shared network namespace).

**Files:**
- Create: `frontend/nginx.prod.conf`

**Step 1: Create the production nginx config**

```nginx
server {
    listen 3000;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    # Strip /scout/ prefix — ALB sends /scout/*, nginx serves without it.
    # Uses a variable so the prefix is configurable via env substitution.
    # SCOUT_PATH_PREFIX is set in the Dockerfile CMD via envsubst.

    # SPA fallback — serves index.html for all non-file routes
    location /scout/ {
        alias /usr/share/nginx/html/;
        try_files $uri $uri/ /index.html;
    }

    # Proxy API requests: /scout/api/* → localhost:8000/api/*
    location /scout/api/ {
        proxy_pass http://localhost:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Critical for streaming responses
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }

    # Proxy embed routes: /scout/embed/* → localhost:8000/embed/*
    location /scout/embed/ {
        proxy_pass http://localhost:8000/embed/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Proxy admin: /scout/admin/* → localhost:8000/admin/*
    location /scout/admin/ {
        proxy_pass http://localhost:8000/admin/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Proxy Django allauth/OAuth: /scout/accounts/* → localhost:8000/accounts/*
    location /scout/accounts/ {
        proxy_pass http://localhost:8000/accounts/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Health check (ALB can use /scout/health/)
    location /scout/health/ {
        proxy_pass http://localhost:8000/health/;
    }

    # Gzip
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;
}
```

**Step 2: Commit**

```bash
git add frontend/nginx.prod.conf
git commit -m "feat: add production nginx config with /scout/ prefix stripping"
```

---

### Task 6: Update Dockerfile.frontend for connect-labs builds

Accept build args for the base path and allow selecting the nginx config.

**Files:**
- Modify: `Dockerfile.frontend`

**Step 1: Update Dockerfile.frontend**

```dockerfile
# syntax=docker/dockerfile:1

# --- Build stage ---
FROM oven/bun:1 AS build

ARG VITE_BASE_PATH=/

WORKDIR /app

# Install dependencies first (cache layer)
COPY frontend/package.json frontend/bun.lock* ./
RUN bun install --frozen-lockfile

# Copy source and build
COPY frontend/ .
ENV VITE_BASE_PATH=$VITE_BASE_PATH
RUN bun run build

# --- Production stage ---
FROM nginx:alpine

ARG NGINX_CONF=frontend/nginx.conf

# Copy built assets
COPY --from=build /app/dist /usr/share/nginx/html

# Copy nginx config
COPY ${NGINX_CONF} /etc/nginx/conf.d/default.conf

EXPOSE 3000

CMD ["nginx", "-g", "daemon off;"]
```

**Step 2: Test local build still works**

Run: `docker build -f Dockerfile.frontend -t scout-frontend-test .`

**Step 3: Test connect-labs build**

Run: `docker build -f Dockerfile.frontend --build-arg VITE_BASE_PATH=/scout/ --build-arg NGINX_CONF=frontend/nginx.prod.conf -t scout-frontend-labs .`

**Step 4: Commit**

```bash
git add Dockerfile.frontend
git commit -m "feat: add build args to Dockerfile.frontend for base path and nginx config"
```

---

### Task 7: Create GitHub Actions deploy workflow

**Files:**
- Create: `.github/workflows/deploy-labs.yml`

**Step 1: Create the workflow**

```yaml
name: Deploy to Connect-Labs

on:
  workflow_dispatch:
    inputs:
      run_migrations:
        description: 'Run database migrations before deployment'
        type: boolean
        required: true
        default: false

env:
  AWS_REGION: us-east-1
  ECS_CLUSTER: labs-jj-cluster
  ECS_WEB_SERVICE: labs-jj-scout-web
  ECS_MCP_SERVICE: labs-jj-scout-mcp
  ECR_REGISTRY: 858923557655.dkr.ecr.us-east-1.amazonaws.com
  ECR_BACKEND: labs-jj-scout
  ECR_FRONTEND: labs-jj-scout-frontend

jobs:
  deploy:
    name: Deploy to Fargate
    runs-on: ubuntu-latest

    permissions:
      id-token: write
      contents: read

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Login to Amazon ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build and push backend image
        uses: docker/build-push-action@v5
        with:
          context: .
          file: Dockerfile
          push: true
          tags: |
            ${{ env.ECR_REGISTRY }}/${{ env.ECR_BACKEND }}:latest
            ${{ env.ECR_REGISTRY }}/${{ env.ECR_BACKEND }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Build and push frontend image
        uses: docker/build-push-action@v5
        with:
          context: .
          file: Dockerfile.frontend
          push: true
          build-args: |
            VITE_BASE_PATH=/scout/
            NGINX_CONF=frontend/nginx.prod.conf
          tags: |
            ${{ env.ECR_REGISTRY }}/${{ env.ECR_FRONTEND }}:latest
            ${{ env.ECR_REGISTRY }}/${{ env.ECR_FRONTEND }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Run database migrations
        if: ${{ github.event.inputs.run_migrations == 'true' }}
        run: |
          TASK_DEF_ARN=$(aws ecs describe-task-definition \
            --task-definition ${{ env.ECS_WEB_SERVICE }} \
            --query 'taskDefinition.taskDefinitionArn' \
            --output text)

          TASK_ARN=$(aws ecs run-task \
            --cluster ${{ env.ECS_CLUSTER }} \
            --task-definition $TASK_DEF_ARN \
            --launch-type FARGATE \
            --network-configuration "awsvpcConfiguration={subnets=[${{ secrets.LABS_SUBNET }}],securityGroups=[${{ secrets.LABS_SECURITY_GROUP }}],assignPublicIp=ENABLED}" \
            --overrides '{"containerOverrides":[{"name":"api","command":["python","manage.py","migrate","--noinput"]}]}' \
            --query 'tasks[0].taskArn' \
            --output text)

          echo "Migration task: $TASK_ARN"

          aws ecs wait tasks-stopped \
            --cluster ${{ env.ECS_CLUSTER }} \
            --tasks $TASK_ARN

          EXIT_CODE=$(aws ecs describe-tasks \
            --cluster ${{ env.ECS_CLUSTER }} \
            --tasks $TASK_ARN \
            --query 'tasks[0].containers[0].exitCode' \
            --output text)

          if [ "$EXIT_CODE" == "0" ]; then
            echo "Migrations completed successfully"
          else
            echo "Migrations failed with exit code: $EXIT_CODE"
            exit 1
          fi

      - name: Deploy web service
        run: |
          aws ecs update-service \
            --cluster ${{ env.ECS_CLUSTER }} \
            --service ${{ env.ECS_WEB_SERVICE }} \
            --force-new-deployment \
            --output text

      - name: Deploy MCP service
        run: |
          aws ecs update-service \
            --cluster ${{ env.ECS_CLUSTER }} \
            --service ${{ env.ECS_MCP_SERVICE }} \
            --force-new-deployment \
            --output text

      - name: Wait for services to stabilize
        run: |
          aws ecs wait services-stable \
            --cluster ${{ env.ECS_CLUSTER }} \
            --services ${{ env.ECS_WEB_SERVICE }} ${{ env.ECS_MCP_SERVICE }}
```

**Step 2: Commit**

```bash
git add .github/workflows/deploy-labs.yml
git commit -m "feat: add GitHub Actions deploy workflow for connect-labs"
```

---

### Task 8: Create AWS setup guide

Document the one-time AWS setup steps so they can be reproduced.

**Files:**
- Create: `docs/deploy-connectlabs-setup.md`

**Step 1: Create the guide**

```markdown
# Connect-Labs AWS Setup Guide

One-time manual setup steps for deploying Scout to the connect-labs
AWS environment. After this, deployments are automated via GitHub Actions.

## Prerequisites

- AWS CLI configured with access to account 858923557655
- Access to the `labs-jj-cluster` ECS cluster
- RDS and ElastiCache endpoints from connect-labs

## 1. ECR Repositories

```bash
aws ecr create-repository --repository-name labs-jj-scout --region us-east-1
aws ecr create-repository --repository-name labs-jj-scout-frontend --region us-east-1
```

## 2. RDS Database

Connect to the existing RDS instance and create the Scout database:

```sql
CREATE DATABASE scout_labs;
```

## 3. GitHub Repository Secrets

Add these secrets to the Scout GitHub repo (Settings → Secrets → Actions):

| Secret | Value |
|--------|-------|
| `AWS_ROLE_ARN` | Same OIDC role ARN as connect-labs |
| `LABS_SUBNET` | Same subnet as connect-labs (e.g., `subnet-06646effb09be2f42`) |
| `LABS_SECURITY_GROUP` | Security group allowing RDS/Redis/ALB access |

## 4. ECS Task Definitions

Create two task definitions via the AWS console or CLI.

### labs-jj-scout-web

- Launch type: Fargate
- CPU: 0.5 vCPU, Memory: 1 GB
- Network mode: awsvpc
- Two containers:

**Container: `frontend`** (essential)
- Image: `858923557655.dkr.ecr.us-east-1.amazonaws.com/labs-jj-scout-frontend:latest`
- Port: 3000
- This container is the target for the ALB

**Container: `api`** (essential)
- Image: `858923557655.dkr.ecr.us-east-1.amazonaws.com/labs-jj-scout:latest`
- Port: 8000
- Command: `sh -c "python manage.py migrate && uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 2 --timeout-keep-alive 120"`
- Environment variables:
  - `DATABASE_URL` = `postgresql://USER:PASS@RDS_HOST:5432/scout_labs`
  - `DJANGO_SETTINGS_MODULE` = `config.settings.connectlabs`
  - `DJANGO_SECRET_KEY` = (generate a new one)
  - `DB_CREDENTIAL_KEY` = (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
  - `ANTHROPIC_API_KEY` = (your key)
  - `REDIS_URL` = `redis://ELASTICACHE_HOST:6379/1`
  - `MCP_SERVER_URL` = `http://scout-mcp.labs-local:8100/mcp`
  - `DJANGO_ALLOWED_HOSTS` = `labs.connect.dimagi.com`
  - `CSRF_TRUSTED_ORIGINS` = `https://labs.connect.dimagi.com`
  - `EMBED_ALLOWED_ORIGINS` = `https://labs.connect.dimagi.com`
  - `FORCE_SCRIPT_NAME` = `/scout`
  - `SECURE_SSL_REDIRECT` = `False`

### labs-jj-scout-mcp

- Launch type: Fargate
- CPU: 0.25 vCPU, Memory: 512 MB
- Network mode: awsvpc
- One container:

**Container: `mcp-server`**
- Image: `858923557655.dkr.ecr.us-east-1.amazonaws.com/labs-jj-scout:latest`
- Port: 8100
- Command: `python -m mcp_server --transport streamable-http --host 0.0.0.0 --port 8100`
- Environment variables:
  - `DATABASE_URL` = `postgresql://USER:PASS@RDS_HOST:5432/scout_labs`
  - `DJANGO_SETTINGS_MODULE` = `config.settings.connectlabs`
  - `DJANGO_SECRET_KEY` = (same as web)
  - `DB_CREDENTIAL_KEY` = (same as web)

## 5. ECS Services

Create two services on `labs-jj-cluster`:

### labs-jj-scout-mcp
- Task definition: `labs-jj-scout-mcp`
- Desired count: 1
- Enable Service Connect or Cloud Map with namespace `labs-local`,
  service name `scout-mcp` → this creates the `scout-mcp.labs-local` hostname

### labs-jj-scout-web
- Task definition: `labs-jj-scout-web`
- Desired count: 1
- Load balancer: existing ALB
- Target group: `labs-jj-scout-tg` (create new, port 3000, health check `/scout/health/`)

## 6. ALB Listener Rule

On the existing HTTPS:443 listener for `labs.connect.dimagi.com`:

- Condition: Path pattern = `/scout/*`
- Action: Forward to `labs-jj-scout-tg`
- Priority: Higher than the default rule (e.g., 10)

## 7. Security Groups

Ensure the Scout security group allows:
- Inbound: ALB security group → port 3000 (frontend)
- Outbound: RDS security group → port 5432
- Outbound: ElastiCache security group → port 6379
- Between Scout services: port 8100 (MCP)
```

**Step 2: Commit**

```bash
git add docs/deploy-connectlabs-setup.md
git commit -m "docs: add connect-labs AWS setup guide"
```

---

### Task 9: Verify full build pipeline locally

**Step 1: Build backend image**

Run: `docker build -t scout-backend-test .`

Expected: Builds successfully.

**Step 2: Build frontend image with connect-labs args**

Run: `docker build -f Dockerfile.frontend --build-arg VITE_BASE_PATH=/scout/ --build-arg NGINX_CONF=frontend/nginx.prod.conf -t scout-frontend-test .`

Expected: Builds successfully.

**Step 3: Verify frontend assets have correct base path**

Run: `docker run --rm scout-frontend-test cat /usr/share/nginx/html/index.html | head -20`

Expected: Asset references use `/scout/assets/...`.

**Step 4: Verify nginx config is correct**

Run: `docker run --rm scout-frontend-test cat /etc/nginx/conf.d/default.conf`

Expected: Shows the `nginx.prod.conf` with `/scout/` location blocks.

**Step 5: Run Python linter**

Run: `uv run ruff check config/settings/connectlabs.py`

Expected: No errors.

**Step 6: Run frontend linter**

Run: `cd frontend && bun run lint`

Expected: No errors.

**Step 7: Final commit if any fixups needed, then push**

```bash
git push -u origin jjackson/deploy-connectlabs
```
