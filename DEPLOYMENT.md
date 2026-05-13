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
| `SCOUT_RDS_ENDPOINT` | CloudFormation output `RDSEndpoint` |
| `SCOUT_VITE_SENTRY_DSN` | Sentry → frontend project → Client Keys (DSN). Baked into the frontend bundle at build time; safe to expose. |
| `SCOUT_SENTRY_AUTH_TOKEN` | Sentry auth token with `project:releases` + `project:write` scopes. Used only at build time to upload source maps. |

**Variables** (Settings > Variables > Actions):

| Variable | Source |
|----------|--------|
| `SCOUT_ECR_REGISTRY` | CloudFormation output `ECRRegistry` |
| `SCOUT_SENTRY_ORG` | Sentry org slug (e.g. `dimagi`). |
| `SCOUT_SENTRY_FRONTEND_PROJECT` | Sentry project slug for the React app (e.g. `scout-frontend`). |

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
| `SCOUT_SENTRY_DSN` | Sentry DSN for the backend Django project (API, worker, MCP all share it) |

The RDS master password is auto-managed by AWS (referenced via `SCOUT_RDS_SECRET_ARN`).
`DATABASE_URL` is resolved at deploy time by `scripts/resolve-database-url.sh`.

## Error monitoring (Sentry)

Sentry is wired in for both the backend (API + worker + MCP via `config/settings/base.py`)
and the frontend (`frontend/src/main.tsx`), but it's fully opt-in: with no DSN set, the
SDKs never initialize. To turn it on:

1. Create two Sentry projects — `python-django` for the backend and `react` for the frontend.
2. Add the backend DSN to AWS Secrets Manager as `SCOUT_SENTRY_DSN`.
3. Add the frontend DSN and a source-map auth token to GitHub Actions secrets
   (`SCOUT_VITE_SENTRY_DSN`, `SCOUT_SENTRY_AUTH_TOKEN`). Frontend DSNs are public by
   design — they ship in the browser bundle — so a GH variable would also work; the
   auth token must be a secret.
4. Set `SCOUT_SENTRY_ORG` and `SCOUT_SENTRY_FRONTEND_PROJECT` as GH Actions variables.

Once the secrets exist, the next push to `main` turns Sentry on for all four services.
To disable without a redeploy, blank the `SCOUT_SENTRY_DSN` secret in AWS.

Tunable via the `env.clear` block in each Kamal config (or by editing and redeploying):

| Variable | Default | Notes |
|----------|---------|-------|
| `SENTRY_ENVIRONMENT` | `production` | Shows up as the event's environment tag. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` | Fraction of requests to trace for performance. `0.0` = errors only. |
| `SENTRY_RELEASE` | `$IMAGE_TAG` | Set automatically to the commit SHA by CI so stack frames match the right build. |
| `SENTRY_SEND_DEFAULT_PII` | `False` | Leave off unless you've reviewed what sentry-sdk captures. |

**Source maps.** The frontend Docker build runs `@sentry/vite-plugin` when all three of
`SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` are present at build time. It emits
hidden source maps, uploads them to Sentry tagged with the release (git SHA), then
deletes them from `dist/` so they don't ship to browsers. The auth token is passed as a
BuildKit secret (`--secret id=sentry_auth_token`) and never lands in an image layer.

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
# View logs (CloudWatch)
#
# Container stdout/stderr ships to CloudWatch Logs in us-east-1. There is
# one log group per service; each container instance is its own stream.
#
# Log groups: /scout/api, /scout/mcp, /scout/worker, /scout/frontend
#
# Tail live:
aws logs tail /scout/api     --follow --profile scout --region us-east-1
aws logs tail /scout/worker  --follow --profile scout --region us-east-1
aws logs tail /scout/mcp     --follow --profile scout --region us-east-1

# Last 15 minutes:
aws logs tail /scout/api    --since 15m --profile scout --region us-east-1
aws logs tail /scout/worker --since 15m --profile scout --region us-east-1
#
# CloudWatch Logs Insights queries: https://console.aws.amazon.com/cloudwatch/
#
# Note: `kamal app logs` shows nothing under the awslogs driver — Docker's
# `logs` command only works for the json-file/journald drivers. Use the
# `aws logs tail` commands above instead.

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

### Rolling back the CloudWatch logging driver

If you need to revert a service to Docker's default `json-file` log driver (e.g., the
`awslogs` driver is preventing containers from starting):

1. Remove the `logging:` block from the relevant `config/deploy*.yml`.
2. Redeploy the affected service(s):
   ```bash
   kamal deploy -c config/deploy.yml
   kamal deploy -c config/deploy-worker.yml
   # repeat for any other affected service
   ```
3. Containers restart under the `json-file` driver; `kamal app logs` and
   `docker logs` work again immediately.

Existing CloudWatch log groups (`/scout/api`, `/scout/worker`, etc.) and all
historical streams are preserved with their 30-day retention — no data is lost.

## Infrastructure Changes

The CloudFormation stack is at `infra/scout-stack.yml`. To update:

```bash
aws cloudformation update-stack \
  --stack-name scout-production \
  --template-body file://infra/scout-stack.yml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=EC2KeyPairName,UsePreviousValue=true \
  --profile scout \
  --region us-east-1
```

After infra changes, re-run `./scripts/fetch-deploy-env.sh` and update GitHub secrets
if any outputs changed.

**CloudWatch log groups must exist before deploying containers.** The Kamal
`logging:` blocks set `awslogs-create-group: "false"`, so Docker will not create
the groups automatically. When a commit bundles both CFN changes (new/updated log
groups) and Kamal `logging:` block changes, always run `aws cloudformation
update-stack` (and wait for it to complete) before running `kamal deploy`. If
the stack update is skipped, containers will fail to start because the log driver
cannot find its target group.
