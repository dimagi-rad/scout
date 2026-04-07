# Deployment

Scout deploys to a single EC2 instance on AWS using [Kamal](https://kamal-deploy.org/).
Pushes to `main` trigger an automated deployment via GitHub Actions.

## Architecture

- **EC2** (t3.medium) — runs all containers via Docker/Kamal
- **RDS PostgreSQL 16** — platform database (password managed by AWS Secrets Manager)
- **ElastiCache Redis 7** — caching and Celery broker
- **ECR** — Docker image registry (scout/api, scout/mcp, scout/frontend)
- **GitHub OIDC** — keyless authentication for CI/CD (no long-lived IAM keys)

All infrastructure is defined in `infra/scout-stack.yml` (CloudFormation) and deployed
as the `scout-production` stack in `us-east-1`.

### Services (Kamal configs in `config/`)

| Service | Config | Port | Public? |
|---------|--------|------|---------|
| API (Django/uvicorn) | `deploy.yml` | 8000 | No (internal network) |
| MCP Server | `deploy-mcp.yml` | 8100 | No (internal network) |
| Worker (Celery) | `deploy-worker.yml` | — | No |
| Frontend (nginx) | `deploy-frontend.yml` | 443 | Yes (sole public entry point) |

The frontend nginx container reverse-proxies `/api/` and `/mcp/` to the internal services.

## Automated Deployment (CI/CD)

The GitHub Actions workflow (`.github/workflows/deploy.yml`) runs on every push to `main`:

1. Authenticates to AWS via OIDC (no access keys)
2. Builds and pushes Docker images to ECR
3. Deploys each service with Kamal
4. Runs migrations in a pre-deploy hook (API service only)

### Required GitHub Configuration

**Secrets** (Settings > Secrets > Actions):

| Secret | Source |
|--------|--------|
| `SCOUT_GITHUB_DEPLOY_ROLE_ARN` | CloudFormation output `GitHubDeployRoleArn` |
| `SSH_PRIVATE_KEY` | `scout-deploy` key pair (1Password: "scout prod ec2 SSH Key" in "GSO: Open Chat Studio Team (OCS)") |
| `SCOUT_EC2_IP` | CloudFormation output `EC2PublicIP` |
| `SCOUT_REDIS_ENDPOINT` | CloudFormation output `RedisEndpoint` |
| `SCOUT_RDS_SECRET_ARN` | CloudFormation output `RDSSecretArn` |
| `ANTHROPIC_API_KEY` | Anthropic dashboard |

**Variables** (Settings > Variables > Actions):

| Variable | Source |
|----------|--------|
| `SCOUT_ECR_REGISTRY` | CloudFormation output `ECRRegistry` |

### AWS Secrets Manager

The deploy scripts fetch app secrets from two Secrets Manager entries:

| Secret ID | Keys |
|-----------|------|
| `scout/django` | `SCOUT_DJANGO_SECRET_KEY`, `SCOUT_DB_CREDENTIAL_KEY` |
| `scout/api-keys` | `SCOUT_ANTHROPIC_API_KEY`, `SCOUT_SENTRY_DSN` (optional) |

The RDS master password is auto-managed by AWS (referenced via `SCOUT_RDS_SECRET_ARN`).

## Manual Deployment

For deploying from your local machine (e.g., debugging or first-time setup):

### Prerequisites

1. **1Password CLI** — used to access the SSH key for deploys:
   - Install: https://developer.1password.com/docs/cli/get-started/
   - Do **not** use Flatpak or Snap — they don't work with the SSH agent.
   - Configure the SSH agent in `~/.config/1Password/ssh/agent.toml`:
     ```toml
     [[ssh-keys]]
     vault = "GSO: Open Chat Studio Team (OCS)"
     ```
   - See https://developer.1password.com/docs/ssh/agent for details.
   - If you don't have access to this vault, have your public key added to the EC2 instance.

2. **AWS CLI** with SSO configured:
   ```bash
   aws configure sso --profile scout
   aws sso login --profile scout
   ```

3. **SSH key**: `~/.ssh/scout-deploy.pem` (from 1Password: "scout prod ec2 SSH Key" in "GSO: Open Chat Studio Team (OCS)")

4. **Ruby + Kamal**: `gem install kamal`

### Steps

```bash
# 1. Generate .env.deploy from CloudFormation outputs
./scripts/fetch-deploy-env.sh

# 2. Deploy (first time)
kamal setup

# 3. Deploy (subsequent)
kamal deploy

# Or deploy a specific service
kamal deploy -c config/deploy-mcp.yml
kamal deploy -c config/deploy-frontend.yml
kamal deploy -c config/deploy-worker.yml
```

## Useful Commands

```bash
# View logs
kamal app logs                    # API logs
kamal app logs -c config/deploy-mcp.yml  # MCP logs

# SSH into a container
kamal app exec -i -- bash

# Restart a service
kamal app restart
kamal app restart -c config/deploy-frontend.yml

# Check deployment status
kamal details

# Run Django management commands
kamal app exec -- python manage.py shell
kamal app exec -- python manage.py migrate
```

## Infrastructure Changes

The CloudFormation stack is at `infra/scout-stack.yml`. To update:

```bash
aws cloudformation update-stack \
  --stack-name scout-production \
  --template-body file://infra/scout-stack.yml \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile scout \
  --region us-east-1
```

After infra changes, re-run `./scripts/fetch-deploy-env.sh` and update GitHub secrets
if any outputs changed.
