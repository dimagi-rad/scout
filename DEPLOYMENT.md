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

### AWS Secrets Manager

The deploy pipeline fetches these secrets from AWS Secrets Manager via Kamal's
`aws_secrets_manager` adapter (see `.kamal/secrets`):

| Secret | Purpose |
|--------|---------|
| `COMMCARE_OAUTH_CLIENT_ID` | CommCare HQ OAuth |
| `COMMCARE_OAUTH_CLIENT_SECRET` | CommCare HQ OAuth |
| `CONNECT_OAUTH_CLIENT_ID` | CommCare Connect OAuth |
| `CONNECT_OAUTH_CLIENT_SECRET` | CommCare Connect OAuth |
| `SCOUT_LANGFUSE_SECRET_KEY` | Langfuse observability |
| `SCOUT_LANGFUSE_PUBLIC_KEY` | Langfuse observability |
| `SCOUT_DJANGO_SECRET_KEY` | Django secret key |
| `SCOUT_DB_CREDENTIAL_KEY` | Fernet key for DB credential encryption |
| `SCOUT_ANTHROPIC_API_KEY` | Claude API key |

The RDS master password is auto-managed by AWS (referenced via `SCOUT_RDS_SECRET_ARN`).
`DATABASE_URL` is resolved at deploy time by `scripts/resolve-database-url.sh`.

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

3. **SSH key** loaded into your SSH agent. Either:
   - Use the **1Password SSH agent** (recommended, configured above), or
   - Manually add the key: `ssh-add ~/.ssh/scout-deploy.pem`
     (download from 1Password: "scout prod ec2 SSH Key" in "GSO: Open Chat Studio Team (OCS)")

4. **Ruby + Kamal**: `gem install kamal`

### Steps

```bash
# 1. Generate .env.deploy from CloudFormation outputs
./scripts/fetch-deploy-env.sh        # use -q/--quiet to suppress output

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
kamal app exec -- python manage.py setup_oauth_apps --domain scout.dimagi.com

# Resolve DATABASE_URL from AWS Secrets Manager (no caching)
./scripts/resolve-database-url.sh

# Debug Kamal secrets
kamal secrets print
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
