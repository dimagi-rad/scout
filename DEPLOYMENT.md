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

> **This describes the Scout *production* environment only.** The CloudFormation
> template above is **not** a source of truth for the connect-labs deployment —
> see [Connect-labs (ECS Fargate)](#connect-labs-ecs-fargate) below.

### Connect-labs (ECS Fargate)

Scout also runs on the **connect-labs** environment, which is a separate deploy
target with **no in-repo IaC** (issue #248, finding 11#1):

- **Compute:** ECS Fargate (cluster `labs-jj-cluster`; services
  `labs-jj-scout-web`, `labs-jj-scout-mcp`, `labs-jj-scout-worker`) — **not**
  EC2/Kamal like production.
- **AWS account:** `858923557655` (ECR registry
  `858923557655.dkr.ecr.us-east-1.amazonaws.com`) — distinct from the
  Scout-production account.
- **Path prefix:** served under `/scout` via `FORCE_SCRIPT_NAME`
  (`config/settings/connectlabs.py`) and the nginx `/scout/...` locations
  (`frontend/nginx.prod.conf`). Frontend builds set `VITE_BASE_PATH=/scout`.
- **Deploy workflow:** `.github/workflows/deploy-labs.yml` only **builds and
  pushes images and updates the ECS services**. The Fargate task definitions,
  ALB, VPC/networking, and IAM roles for that account are managed **outside this
  repository** and are not represented in `infra/scout-stack.yml`.

Because of this drift, changes to the labs runtime topology (task definitions,
ALB routing, scaling) must be made in that external infrastructure, not in this
repo's CloudFormation template.

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
| `SCOUT_TASKBADGER_API_KEY` | Task Badger project API key for background-job tracking (API + worker share it) |

The RDS master password is auto-managed by AWS (referenced via `SCOUT_RDS_SECRET_ARN`).
`DATABASE_URL` is resolved at deploy time by `scripts/resolve-database-url.sh`.

Connect staging is a separate OAuth provider and does not use the two production
AWS secrets above. Store its application credentials as
`SCOUT_STAGING_CONNECT_OAUTH_CLIENT_ID` and
`SCOUT_STAGING_CONNECT_OAUTH_CLIENT_SECRET` in the GitHub `staging` environment.
The staging workflow maps them to `STAGING_CONNECT_OAUTH_*` inside the API
container; production continues to use the AWS-backed `CONNECT_OAUTH_*` values.
Store a random signing key as `SCOUT_STAGING_CUBEJS_API_SECRET` in the same
environment. The workflow shares it only among staging's API, worker, MCP, and
Cube containers so semantic-query security contexts are accepted end to end.

### Adding a new secret

The chain runs AWS Secrets Manager → `.kamal/secrets` → `env.secret` in each Kamal
config. To add one (using `SCOUT_TASKBADGER_API_KEY` as the example):

1. **Store it in AWS Secrets Manager**, following the `SCOUT_*` naming convention:
   ```bash
   aws secretsmanager create-secret \
     --name SCOUT_TASKBADGER_API_KEY \
     --secret-string '<value>' \
     --region us-east-1
   ```
   (Use `put-secret-value` instead of `create-secret` to rotate an existing one.)

2. **Map it in `.kamal/secrets`** — fetch it from AWS, then extract it into the env
   var name your app reads. Group related keys into one `fetch` call to cut AWS round-trips:
   ```bash
   TASKBADGER_SECRETS=$(kamal secrets fetch --adapter aws_secrets_manager SCOUT_TASKBADGER_API_KEY)
   TASKBADGER_API_KEY=$(kamal secrets extract SCOUT_TASKBADGER_API_KEY $TASKBADGER_SECRETS)
   ```

3. **Reference it under `env.secret:`** in every Kamal config whose container needs it
   (`config/deploy.yml`, `config/deploy-worker.yml`, `config/deploy-mcp.yml`):
   ```yaml
   env:
     secret:
       - TASKBADGER_API_KEY
   ```

4. **Verify and deploy.** `kamal secrets print` resolves the file locally so you can
   confirm the value is non-empty before shipping; then redeploy the affected services.

Only `env.secret` entries are injected from `.kamal/secrets`. Non-sensitive config goes
in `env.clear` instead, written inline in the Kamal config (no AWS entry needed).

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

## Second environment (staging)

A staging environment (`scout-staging.dimagi.com`) runs **co-located on the
production EC2 host** for testing branches. It reuses every AWS Secrets Manager
value, the ECR repos, and the RDS *instance* — but has **its own database**
(`agent_platform_staging`) and its own Docker network (`scout_staging_shared`),
so its data and internal services are isolated from production. Config lives in
`config/deploy-staging*.yml`.

One thing is *not* isolated: PostgreSQL roles are cluster-scoped, not per-database.
The `<schema>_ro` / `<schema>_dbt` roles `SchemaManager` mints are named
deterministically from `(provider, external_id)`, so a tenant provisioned in both
environments shares a single role object. Expect `DROP ROLE` during schema teardown
to fail with "objects depend on it … in database agent_platform_staging" (or vice
versa) and leave a dangling role — the teardown swallows and logs it, so it is
noise rather than breakage, but it is why staging role errors can appear in
production logs.

Notes: it runs the API with 2 uvicorn workers (not 4) and no Redis (LocMemCache)
to limit its footprint on the shared t3.medium, and uses Docker's `json-file` log
driver so `kamal app logs` works directly.

### One-time setup

1. **Create the database** on the existing RDS instance (uses the prod master
   role; run from a machine with AWS access):
   ```bash
   source .env.deploy
   DATABASE_URL=$(SCOUT_DB_NAME=postgres ./scripts/resolve-database-url.sh)
   psql "$DATABASE_URL" -c "CREATE DATABASE agent_platform_staging;"
   ```
2. **DNS**: add an A record `scout-staging.dimagi.com` → the EC2 Elastic IP
   (`SCOUT_EC2_IP` in `.env.deploy`). Kamal's proxy issues the TLS cert once the
   record resolves.
3. **OAuth**: most providers reuse the production OAuth client IDs, so register
   the staging callback URLs
   (`https://scout-staging.dimagi.com/accounts/<provider>/login/callback/`) with
   those providers. Connect is the exception: create a confidential authorization
   code application on `https://connect-staging.dimagi.com/o/applications/` with
   callback URL
   `https://scout-staging.dimagi.com/accounts/commcare_connect/login/callback/`,
   then store its credentials in the two GitHub `staging` environment secrets
   documented above. `setup_oauth_apps` runs automatically for the staging domain
   in the API container's entrypoint.

### Deploying from GitHub Actions

Run the **Deploy Scout (Staging)** workflow and pick the branch to deploy from the
ref dropdown. It builds and pushes the API, frontend, and Cube images, then deploys
Cube → MCP → API → worker → frontend. In addition to the production deploy secrets,
the GitHub `staging` environment must contain the two Connect-staging OAuth secrets
and `SCOUT_STAGING_CUBEJS_API_SECRET` documented above.

Tests are not a gate — staging is for trying work in progress. The workflow is
`workflow_dispatch`-only, so nothing reaches staging unless someone asks for it.

Branch deploys depend on a GitHub **environment named `staging`** existing in repo
settings (Settings → Environments). The job declares `environment: staging` purely
to change its OIDC token's `sub` claim to `repo:dimagi-rad/scout:environment:staging`,
which is what `scout-github-deploy` trusts for non-main refs — production's trust
stays pinned to `refs/heads/main`. Leave the environment's *Deployment branches*
setting on "All branches" so any work-in-progress branch can deploy; add required
reviewers there if staging ever needs an approval step. Without the environment,
every branch deploy fails at `AssumeRoleWithWebIdentity` with "not authorized to
perform sts:AssumeRoleWithWebIdentity".

Frontend images are tagged `staging-<sha>` rather than `<sha>`: the image bakes in
`nginx.staging-kamal.conf` and `SENTRY_ENVIRONMENT` at build time, so sharing a tag
with production would mean whichever environment deployed a given commit last wins.
The API image carries no environment-specific build args and reuses the plain `<sha>`.
Staging frontend builds skip the Sentry sourcemap upload, so a staging deploy can't
overwrite the artifacts of a production release with the same SHA — errors still
report to Sentry under the `staging` environment.

### Deploying from your machine

```bash
git checkout codex/semantic-model-work
source .env.deploy && source config/staging.env

# First time
kamal setup -c config/deploy-staging-cube.yml --version=cube-$(git rev-parse HEAD)
kamal setup -c config/deploy-staging-mcp.yml
kamal setup -c config/deploy-staging.yml
kamal setup -c config/deploy-staging-worker.yml
kamal setup -c config/deploy-staging-frontend.yml --version=staging-$(git rev-parse HEAD)

# Subsequent deploys
kamal deploy -c config/deploy-staging-cube.yml --version=cube-$(git rev-parse HEAD)
kamal deploy -c config/deploy-staging-mcp.yml
kamal deploy -c config/deploy-staging.yml
kamal deploy -c config/deploy-staging-worker.yml
kamal deploy -c config/deploy-staging-frontend.yml --version=staging-$(git rev-parse HEAD)
```

The frontend commands carry an explicit `--version`. Without it Kamal versions the
build as the bare git SHA and pushes it as `scout/frontend:<sha>` — the same tag
production uses — but with `nginx.staging-kamal.conf` baked in. A later production
`kamal rollback`, host reboot, or re-pull of that version would then serve a
frontend proxying to `scout-staging-web`, putting production traffic on the staging
API. The API/MCP/worker image is environment-agnostic, so those need no override.

Migrations run automatically against the staging database when the API container
starts. Logs: `kamal app logs -c config/deploy-staging.yml`.

> Always `source config/staging.env` before staging commands — it points
> `DATABASE_URL` at the staging database. A plain `source .env.deploy` (prod)
> would deploy staging containers against the **production** database.
>
> And `unset SCOUT_DB_NAME` before running any **production** kamal command in
> that shell. The export survives a re-`source` of `.env.deploy` (which never
> sets it), so a prod deploy from the same session resolves `DATABASE_URL` to
> `agent_platform_staging` and points production at the staging database.

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
