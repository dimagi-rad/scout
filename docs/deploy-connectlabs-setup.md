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

Add these secrets to the Scout GitHub repo (Settings > Secrets > Actions):

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
  service name `scout-mcp` — this creates the `scout-mcp.labs-local` hostname

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
- Inbound: ALB security group -> port 3000 (frontend)
- Outbound: RDS security group -> port 5432
- Outbound: ElastiCache security group -> port 6379
- Between Scout services: port 8100 (MCP)
