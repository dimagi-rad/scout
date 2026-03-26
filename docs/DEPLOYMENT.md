# Scout Production Deployment Spec

Production deployment of Scout on a dedicated AWS account: CloudFormation for AWS infrastructure (including EC2 setup via UserData), Kamal for container orchestration, and AWS Secrets Manager for credential storage.

## Architecture Overview

Four application containers deployed via Kamal onto a single EC2 instance, with RDS PostgreSQL and ElastiCache Redis as managed services. Kamal's built-in proxy handles TLS termination and routing.

| Container | Port | Public? | Description |
|-----------|------|---------|-------------|
| **Django API** | 8000 | No | uvicorn ASGI server |
| **MCP Server** | 8100 | No | FastMCP data access layer |
| **Celery Worker** | -- | No | Worker + Beat for background tasks and periodic scheduling |
| **Frontend** | 3000 | Yes | nginx serving React SPA, proxies `/api/` and `/admin/` to Django |

Only the frontend container is publicly accessible. The API, MCP, and worker are reachable only via the `scout_shared` Docker network.

### Managed Services (AWS)

- **RDS PostgreSQL 16** -- platform database (password auto-managed by RDS)
- **ElastiCache Redis 7** -- caching, rate limiting, Celery broker
- **ECR** -- Docker image registry
- **Secrets Manager** -- all credentials

### Tooling

- **CloudFormation** -- AWS infrastructure (VPC, RDS, ElastiCache, ECR, SGs, IAM, EC2 + UserData)
- **Kamal 2** -- container deploy + proxy + TLS
- **GitHub Actions** -- CI/CD pipeline (builds images, deploys via Kamal)
- **SSM Session Manager** -- operator shell access (no SSH needed for humans)

### Monitoring

- **BetterStack** -- uptime monitoring on `/health/` endpoint
- **Sentry** -- error tracking (add `SENTRY_DSN` to env)

> **Reference:** This spec follows the `data-buddy` deployment pattern (Kamal + AWS Secrets Manager) adapted for Scout's Django/React/MCP stack.

---

## 1. AWS Account Setup

Dedicated AWS account under the organization. Isolates billing, IAM, and blast radius from other projects.

| Setting | Value |
|---------|-------|
| Account Name | `scout-production` |
| Region | `us-east-1` |
| SSO Profile | `scout` (for AWS CLI / Kamal) |

---

## 2. CloudFormation Infrastructure

A single CloudFormation stack provisions all AWS resources, including EC2 instance configuration via UserData (no Ansible needed).

```bash
# Deploy the stack
aws cloudformation deploy \
  --template-file infra/scout-stack.yml \
  --stack-name scout-production \
  --parameter-overrides \
    EC2KeyPairName=scout-deploy \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile scout

# View outputs (EC2 IP, RDS endpoint, Redis endpoint, etc.)
aws cloudformation describe-stacks \
  --stack-name scout-production \
  --query 'Stacks[0].Outputs' \
  --profile scout
```

### [`infra/scout-stack.yml`](../infra/scout-stack.yml)

See the template file for full resource definitions. Key resources:

| Resource | Type | Notes |
|----------|------|-------|
| VPC + 2 public subnets | Networking | 10.0.0.0/16 CIDR, two AZs |
| EC2 instance | Compute | Ubuntu 24.04, UserData installs Docker + scout user |
| EC2 instance profile | IAM | SSM Session Manager access |
| Elastic IP | Networking | Static IP for DNS and Kamal |
| RDS PostgreSQL 16 | Database | `ManageMasterUserPassword: true`, encrypted, 7-day backups |
| ElastiCache Redis 7.1 | Cache | Single node |
| 3 ECR repos | Registry | `scout/api`, `scout/mcp`, `scout/frontend` with lifecycle policies |
| GitHub OIDC provider + role | IAM | Scoped to `dimagi-rad/scout:main`, ECR push + Secrets Manager read |
| Security groups | Network | SSH/HTTP/HTTPS on EC2; Postgres/Redis restricted to EC2 SG |

### Stack Outputs

After deploying the stack, the outputs provide the values needed by Kamal configs and `.kamal/secrets`:

| Output | Used In |
|--------|---------|
| `EC2PublicIP` | Kamal `servers.web.hosts`, DNS A record |
| `RDSEndpoint` | `.kamal/secrets` → `DATABASE_URL` |
| `RDSSecretArn` | `.kamal/secrets` → fetch DB password from RDS-managed secret |
| `RedisEndpoint` | Kamal `env.clear.REDIS_URL` |
| `ECRRegistry` | Kamal `registry.server` |
| `GitHubDeployRoleArn` | GitHub Actions `role-to-assume` |

### Updating Infrastructure

```bash
# Preview changes
aws cloudformation deploy \
  --template-file infra/scout-stack.yml \
  --stack-name scout-production \
  --no-execute-changeset \
  --profile scout

# Scale up RDS
aws cloudformation deploy \
  --template-file infra/scout-stack.yml \
  --stack-name scout-production \
  --parameter-overrides DBInstanceClass=db.t4g.small \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile scout
```

### Operator Access

SSH is open for Kamal deploys only (key-based auth, no password). For interactive shell access, use SSM Session Manager:

```bash
# Shell into EC2 (no SSH key needed, IAM-authenticated)
aws ssm start-session --target <instance-id> --profile scout

# Port forward to RDS for local psql access
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<RDSEndpoint>"],"portNumber":["5432"],"localPortNumber":["5432"]}' \
  --profile scout
```

---

## 3. AWS Secrets Manager

All secrets stored in AWS Secrets Manager. Kamal fetches them at deploy time via the `aws_secrets_manager` adapter.

### Secrets Layout

| Secret Group | Keys | Used By |
|-------------|------|---------|
| *(RDS-managed)* | Auto-generated JSON with `username`, `password`, `host`, `port` | API, MCP, Worker (composed into DATABASE_URL) |
| `scout/django` | `DJANGO_SECRET_KEY`, `DB_CREDENTIAL_KEY` | API, MCP, Worker |
| `scout/api-keys` | `ANTHROPIC_API_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `SENTRY_DSN` | API, Worker |
| `scout/oauth` | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `COMMCARE_OAUTH_CLIENT_ID`, `COMMCARE_OAUTH_CLIENT_SECRET` | Bootstrap only (not injected at runtime) |

> **RDS-managed secret:** RDS automatically creates and rotates the master password. The `.kamal/secrets` script fetches this secret and parses the JSON to extract the password and host for `DATABASE_URL`.

> **OAuth credentials** are stored in Secrets Manager but are **not injected into containers at runtime**. They are only used by `scripts/setup_oauth_apps.py`, which reads them from env vars and writes `SocialApp` rows into the database. At runtime, django-allauth reads OAuth credentials from the database. Run the setup script on first deploy and after credential rotation.

### `.kamal/secrets`

```bash
# RDS-managed password (JSON: {"username":"platform","password":"...","host":"...","port":5432})
RDS_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id <RDSSecretArn from stack output> \
  --query SecretString --output text \
  --profile scout)

DB_PASSWORD=$(echo "$RDS_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")
DB_HOST=$(echo "$RDS_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['host'])")
DATABASE_URL=postgresql://platform:$DB_PASSWORD@$DB_HOST:5432/agent_platform

# Django secrets
DJANGO_SECRETS=$(kamal secrets fetch \
  --adapter aws_secrets_manager \
  --account scout \
  SCOUT_DJANGO_SECRET_KEY SCOUT_DB_CREDENTIAL_KEY)

DJANGO_SECRET_KEY=$(kamal secrets extract SCOUT_DJANGO_SECRET_KEY $DJANGO_SECRETS)
DB_CREDENTIAL_KEY=$(kamal secrets extract SCOUT_DB_CREDENTIAL_KEY $DJANGO_SECRETS)

# API keys
API_SECRETS=$(kamal secrets fetch \
  --adapter aws_secrets_manager \
  --account scout \
  SCOUT_ANTHROPIC_API_KEY SCOUT_SENTRY_DSN)

ANTHROPIC_API_KEY=$(kamal secrets extract SCOUT_ANTHROPIC_API_KEY $API_SECRETS)
SENTRY_DSN=$(kamal secrets extract SCOUT_SENTRY_DSN $API_SECRETS)
```

---

## 4. Kamal Deployment

Four separate Kamal configs. All services share the `scout_shared` Docker network. CI builds and pushes images to ECR; Kamal deploys pre-built images by tag.

### `.kamal/hooks/pre-deploy`

```bash
#!/bin/bash
# Run migrations in the newly-built API container before swapping
kamal app exec --version=$KAMAL_VERSION -- python manage.py migrate
```

### `.kamal/hooks/pre-connect`

```bash
#!/bin/bash
# Ensure shared Docker network exists before any service deploys
ssh ${SSH_USER}@${KAMAL_HOSTS} \
  "docker network create scout_shared 2>/dev/null || true"
```

### `config/deploy.yml` -- Django API (Primary)

```yaml
service: scout

image: scout/api

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
    cmd: >
      uvicorn config.asgi:application
        --host 0.0.0.0 --port 8000
        --workers 4 --timeout-keep-alive 120

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

env:
  clear:
    DJANGO_SETTINGS_MODULE: config.settings.production
    DJANGO_ALLOWED_HOSTS: "scout.example.com"
    CSRF_TRUSTED_ORIGINS: "https://scout.example.com"
    SECURE_SSL_REDIRECT: "False"
    MCP_SERVER_URL: "http://scout-mcp-web:8100/mcp"
    REDIS_URL: "redis://<ELASTICACHE_ENDPOINT>:6379/0"
    EMBED_ALLOWED_ORIGINS: "https://labs.connect.dimagi.com"
    MAX_CONNECTIONS_PER_PROJECT: 5
    MAX_QUERIES_PER_MINUTE: 60
  secret:
    - DATABASE_URL
    - DJANGO_SECRET_KEY
    - DB_CREDENTIAL_KEY
    - ANTHROPIC_API_KEY
    - SENTRY_DSN

ssh:
  user: scout

# No proxy — API is internal only, reached via nginx in frontend container
```

### `config/deploy-mcp.yml` -- MCP Server

```yaml
service: scout-mcp

image: scout/mcp

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
      network-alias: "scout-mcp-web"
    cmd: >
      python -m mcp_server
        --transport streamable-http
        --host 0.0.0.0 --port 8100

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

env:
  clear:
    DJANGO_SETTINGS_MODULE: config.settings.production
  secret:
    - DATABASE_URL
    - DB_CREDENTIAL_KEY
    - DJANGO_SECRET_KEY

ssh:
  user: scout

# No proxy — MCP is internal only, reached by API via Docker network
```

### `config/deploy-worker.yml` -- Celery Worker + Beat

```yaml
service: scout-worker

image: scout/api

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
    cmd: >
      celery -A config worker --beat -l info
        --scheduler django_celery_beat.schedulers:DatabaseScheduler

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

env:
  clear:
    DJANGO_SETTINGS_MODULE: config.settings.production
    REDIS_URL: "redis://<ELASTICACHE_ENDPOINT>:6379/0"
  secret:
    - DATABASE_URL
    - DJANGO_SECRET_KEY
    - DB_CREDENTIAL_KEY

ssh:
  user: scout

# No proxy — worker has no HTTP interface
```

### `config/deploy-frontend.yml` -- Frontend

```yaml
service: scout-frontend

image: scout/frontend

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
      network-alias: "scout-frontend-web"

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

ssh:
  user: scout

proxy:
  host: scout.example.com
  app_port: 3000
  ssl: true
  forward_headers: true
  healthcheck:
    interval: 5
    path: /
    timeout: 3
```

### `registry_password.sh`

```bash
#!/bin/bash
# ECR auth token for Kamal (same pattern as data-buddy)
AWS_PROFILE=${AWS_PROFILE:-scout}

# Check if we have valid credentials
if ! aws sts get-caller-identity --profile "$AWS_PROFILE" &>/dev/null 2>&1; then
  aws sso login --profile "$AWS_PROFILE" >&2
fi

aws ecr get-login-password \
  --region us-east-1 \
  --profile "$AWS_PROFILE"
```

### Deploy Commands

```bash
# Deploy all four services (order matters on first deploy)
kamal deploy -c config/deploy-mcp.yml           # MCP server (first — API connects to it)
kamal deploy                                    # API (runs migrations via pre-deploy hook)
kamal deploy -c config/deploy-worker.yml        # Worker (needs DB migrations applied)
kamal deploy -c config/deploy-frontend.yml      # Frontend (last — proxies to API)

# Redeploy without rebuild
kamal redeploy
kamal redeploy -c config/deploy-mcp.yml

# Logs
kamal app logs
kamal app logs -c config/deploy-mcp.yml
kamal app logs -c config/deploy-worker.yml

# Shell access (prefer SSM for interactive use)
kamal app exec -i -- bash

# Rollback
kamal rollback
kamal rollback -c config/deploy-mcp.yml
```

---

## 5. Service Configuration

### Environment Variables

| Variable | Source | Services | Value / Notes |
|----------|--------|----------|---------------|
| `DJANGO_SETTINGS_MODULE` | clear | API, MCP, Worker | `config.settings.production` |
| `DJANGO_ALLOWED_HOSTS` | clear | API | Production domain |
| `CSRF_TRUSTED_ORIGINS` | clear | API | `https://scout.example.com` |
| `SECURE_SSL_REDIRECT` | clear | API | `False` (Kamal proxy handles HTTPS redirect) |
| `EMBED_ALLOWED_ORIGINS` | clear | API | `https://labs.connect.dimagi.com` |
| `DATABASE_URL` | secret | API, MCP, Worker | RDS connection string (from RDS-managed secret) |
| `DJANGO_SECRET_KEY` | secret | API, MCP, Worker | Randomly generated |
| `DB_CREDENTIAL_KEY` | secret | API, MCP, Worker | Fernet key for project DB encryption |
| `ANTHROPIC_API_KEY` | secret | API | Claude API key |
| `SENTRY_DSN` | secret | API, Worker | Sentry error tracking |
| `REDIS_URL` | clear | API, Worker | ElastiCache endpoint |
| `MCP_SERVER_URL` | clear | API | `http://scout-mcp-web:8100/mcp` |
| `LANGFUSE_*` | secret | API | Optional tracing |
| `*_OAUTH_*` | Secrets Manager | Bootstrap only | Not in Kamal env -- used by `setup_oauth_apps.py` to write SocialApp rows to DB |

### Docker Networking

All four containers join the `scout_shared` Docker network. Internal DNS names:

| Alias | Service | Port |
|-------|---------|------|
| `scout-web` | API | 8000 |
| `scout-mcp-web` | MCP | 8100 |
| `scout-worker` | Celery | -- |
| `scout-frontend-web` | nginx | 3000 |

### Nginx Configuration

The frontend nginx is the sole public entry point. It proxies `/api/` and `/admin/` to Django via the Docker network. Key change from dev: `proxy_pass http://scout-web:8000` instead of `http://api:8000`.

```nginx
# frontend/nginx.prod-kamal.conf
server {
    listen 3000;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://scout-web:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }

    location /admin/ {
        proxy_pass http://scout-web:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /health/ {
        proxy_pass http://scout-web:8000;
    }

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;
}
```

### Dockerfile Changes

Add `collectstatic` to the API Dockerfile so it runs at build time, not on every container start:

```dockerfile
# After COPY . . and project install
RUN DJANGO_SECRET_KEY=build-placeholder python manage.py collectstatic --noinput
```

---

## 6. DNS & SSL

### DNS Records

| Record | Type | Value |
|--------|------|-------|
| `scout.example.com` | A | `<EC2PublicIP>` |

MCP server, API, and worker are internal only -- no public DNS needed.

### SSL/TLS

Kamal proxy handles TLS termination with auto-provisioned Let's Encrypt certificates. `forward_headers: true` in the proxy config sends `X-Forwarded-Proto: https` to Django, so `SECURE_PROXY_SSL_HEADER` works correctly for secure cookies. `SECURE_SSL_REDIRECT` is set to `False` because Kamal proxy handles the HTTP→HTTPS redirect at the edge.

### Iframe Embedding

Production settings should use `X_FRAME_OPTIONS = "SAMEORIGIN"` (not `"DENY"`) and set `EMBED_ALLOWED_ORIGINS` to allow embedding from `https://labs.connect.dimagi.com`.

---

## 7. CI/CD Pipeline

GitHub Actions workflow. On merge to `main`, build Docker images, push to ECR, deploy via Kamal. Uses OIDC for AWS authentication (no long-lived access keys).

```yaml
# .github/workflows/deploy.yml
name: Deploy Scout

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    env:
      IMAGE_TAG: ${{ github.sha }}
      ECR_REGISTRY: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: <GitHubDeployRoleArn from stack output>
          aws-region: us-east-1

      - name: Login to ECR
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push API image
        run: |
          docker build -t $ECR_REGISTRY/scout/api:$IMAGE_TAG .
          docker push $ECR_REGISTRY/scout/api:$IMAGE_TAG

      - name: Build and push Frontend image
        run: |
          docker build -t $ECR_REGISTRY/scout/frontend:$IMAGE_TAG \
            -f Dockerfile.frontend .
          docker push $ECR_REGISTRY/scout/frontend:$IMAGE_TAG

      - name: Install Kamal
        run: gem install kamal

      - name: Setup SSH
        uses: webfactory/ssh-agent@v0.9.0
        with:
          ssh-private-key: ${{ secrets.SSH_PRIVATE_KEY }}

      - name: Deploy MCP
        run: kamal deploy -c config/deploy-mcp.yml --version=$IMAGE_TAG

      - name: Deploy API
        run: kamal deploy --version=$IMAGE_TAG

      - name: Deploy Worker
        run: kamal deploy -c config/deploy-worker.yml --version=$IMAGE_TAG

      - name: Deploy Frontend
        run: kamal deploy -c config/deploy-frontend.yml --version=$IMAGE_TAG
```

> **Note:** The API and MCP images are the same Docker image (`scout/api`). The MCP container just runs a different command. Only two images need to be built: API and Frontend.

---

## 8. Launch Checklist

### AWS Setup
- [ ] Create dedicated AWS account
- [ ] Configure SSO profile `scout`
- [ ] Create EC2 key pair `scout-deploy`
- [ ] Deploy CloudFormation stack (`infra/scout-stack.yml`)
- [ ] Note stack outputs (EC2 IP, RDS endpoint, RDS secret ARN, Redis endpoint, ECR registry, OIDC role ARN)
- [ ] Store `GitHubDeployRoleArn` and SSH private key in GitHub Actions secrets

### Secrets
- [ ] Generate `DJANGO_SECRET_KEY` and store in Secrets Manager as `scout/django`
- [ ] Generate `DB_CREDENTIAL_KEY` (Fernet) and store in `scout/django`
- [ ] Store `ANTHROPIC_API_KEY` in `scout/api-keys`
- [ ] Store `SENTRY_DSN` in `scout/api-keys`
- [ ] Store OAuth credentials in `scout/oauth` (Google, GitHub, CommCare)
- [ ] Store Langfuse keys in `scout/api-keys` (optional)
- [ ] Verify RDS-managed secret exists (auto-created by CloudFormation)
- [ ] Test `.kamal/secrets` script locally

### Deploy & Verify
- [ ] Point DNS A record to `EC2PublicIP`
- [ ] Update Kamal configs with stack outputs (hosts, registry, Redis URL)
- [ ] Update `.kamal/secrets` with `RDSSecretArn`
- [ ] Update `DJANGO_ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` with actual domain
- [ ] Create `frontend/nginx.prod-kamal.conf` with `proxy_pass http://scout-web:8000`
- [ ] Add `collectstatic` to Dockerfile
- [ ] Run first deploy: MCP → API → Worker → Frontend
- [ ] Verify `/health/` endpoint returns 200
- [ ] Create superuser: `kamal app exec -- python manage.py createsuperuser`
- [ ] Update Django `Site` record domain from `localhost:8000` to production domain (via admin or `kamal app exec`)
- [ ] Bootstrap OAuth: fetch creds from Secrets Manager, run `kamal app exec -- env GOOGLE_OAUTH_CLIENT_ID=... python scripts/setup_oauth_apps.py`
- [ ] Set up BetterStack uptime monitor on `https://scout.example.com/health/`
- [ ] Test login + chat flow end to end

---

## Day 2 Considerations

- Log aggregation (CloudWatch agent or Datadog)
- Database migrations strategy for zero-downtime deploys (expand/contract pattern)
- Backup verification testing (restore RDS snapshot to verify)
- Scaling: second EC2 instance behind ALB if single-instance risk becomes unacceptable
- Email: configure SES if email/password login or notifications are needed
