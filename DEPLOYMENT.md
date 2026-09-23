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
| Cube + schema validator | `deploy-cube.yml` | 4000 / 4010 | No (internal network) |
| API (Django/uvicorn) | `deploy.yml` | 8000 | No (internal network) |
| MCP Server | `deploy-mcp.yml` | 8100 | No (internal network) |
| Worker (Celery) | `deploy-worker.yml` | — | No |
| Frontend (nginx) | `deploy-frontend.yml` | 443 | Yes (sole public entry point) |

The frontend nginx container reverse-proxies `/api/` and `/mcp/` to the internal services.

Each config is the production definition. Staging deploys from the same files with
`-d staging`, which deep-merges the matching `config/<name>.staging.yml` overlay over
it — see [Second environment (staging)](#second-environment-staging).

## Automated Deployment (CI/CD)

The GitHub Actions workflow (`.github/workflows/deploy.yml`) runs on every push to `main`:

1. Authenticates to AWS via OIDC (no access keys)
2. Builds and pushes the frontend once, including its Sentry build inputs; then
   prunes the host and stops if it still lacks disk space (see
   [Host disk full](#host-disk-full)); then builds all three role-qualified
   backend images before interrupting any worker
3. Deploys Cube → graceful old-worker drain → API migration/health → MCP →
   worker → frontend; prebuilt images use `--skip-push`
4. Uses worker-only `kamal redeploy` to preserve stopped-worker drain evidence
   across the co-located destinations; see the retention guidance below

The workflows pin Kamal **2.12.0**, whose destination labels, image validation,
health polling and worker boot behavior have been verified. Revalidate those
contracts before upgrading the deployment tool.

API, MCP, and worker share the `scout/api` repository and code layers, but Kamal
adds a different `service` label to each image. Their exact versions must therefore
be distinct: `production-api-<sha>`, `production-mcp-<sha>`, and
`production-worker-<sha>` (with `staging-` in place of `production-` for staging).
This prevents one role or destination from replacing the image another is pulling.
`IMAGE_TAG` remains the plain commit SHA for Sentry releases; it is not the backend
image version. Deploy and rollback by the exact version, not a shared `latest` alias.

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
`aws_secrets_manager` adapter (see `.kamal/secrets-common`):

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
| `SCOUT_CUBEJS_API_SECRET` | Production-only Cube JWT signing key shared by API, worker, MCP, and Cube. |

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
Production uses the AWS Secrets Manager value `SCOUT_CUBEJS_API_SECRET`, which
the production workflow validates before building and Kamal resolves through
`.kamal/secrets-common`. Generate the two values independently (for example,
`openssl rand -hex 32`) so a staging credential can never sign a production
Cube security context.

### Adding a new secret

The chain runs AWS Secrets Manager → `.kamal/secrets-common` → `env.secret` in each Kamal
config. To add one (using `SCOUT_TASKBADGER_API_KEY` as the example):

1. **Store it in AWS Secrets Manager**, following the `SCOUT_*` naming convention:
   ```bash
   aws secretsmanager create-secret \
     --name SCOUT_TASKBADGER_API_KEY \
     --secret-string '<value>' \
     --region us-east-1
   ```
   (Use `put-secret-value` instead of `create-secret` to rotate an existing one.)

2. **Map it in `.kamal/secrets-common`** — fetch it from AWS, then extract it into the env
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

Only `env.secret` entries are injected from `.kamal/secrets-common`. Non-sensitive config goes
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
so its data and internal services are isolated from production.

Staging has no config files of its own. It deploys the production configs with
`-d staging`, and Kamal deep-merges `config/<name>.staging.yml` over the base — those
overlays hold only what differs (network, hostnames, Sentry environment, the API's
worker count and secret list). Hashes merge key by key; arrays such as `env.secret`
are replaced wholesale. Secrets resolve from `.kamal/secrets-common`, which Kamal
reads for every destination.

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
ref dropdown. It builds and pushes the frontend and all three role-qualified
backend images before draining workers. Kamal builds Cube from
`cube_config/Dockerfile` into the otherwise-unused `scout/mcp` repository;
backend role images use `scout/api` with distinct versions. It deploys
Cube → graceful old-worker drain → API migration/health gate → MCP → worker →
frontend. In addition to the production
deploy secrets, the GitHub `staging` environment must contain the two
Connect-staging OAuth secrets and `SCOUT_STAGING_CUBEJS_API_SECRET` documented above.

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
Backend versions are `staging-api-<sha>`, `staging-mcp-<sha>`, and
`staging-worker-<sha>` so each role's service label stays attached to its own image.
Staging frontend builds skip the Sentry sourcemap upload, so a staging deploy can't
overwrite the artifacts of a production release with the same SHA — errors still
report to Sentry under the `staging` environment.

### Deploying from your machine

```bash
git checkout codex/semantic-model-work
source .env.deploy && source config/staging.env
export IMAGE_TAG=$(git rev-parse HEAD)
```

Choose **one** sequence below. Each runs in a fail-fast subshell: a failed drain,
migration, or health gate stops that sequence before later services deploy.

First-time setup:

```bash
(
set -e
kamal build push -d staging --version="staging-api-$IMAGE_TAG"
kamal build push -c config/deploy-mcp.yml -d staging --version="staging-mcp-$IMAGE_TAG"
kamal build push -c config/deploy-worker.yml -d staging --version="staging-worker-$IMAGE_TAG"
kamal setup -c config/deploy-cube.yml -d staging --version="cube-$IMAGE_TAG"
ssh -T "scout@$SCOUT_EC2_IP" bash -s -- staging 600 < scripts/drain-workers.sh
kamal setup -d staging --skip-push --version="staging-api-$IMAGE_TAG"
kamal setup -c config/deploy-mcp.yml -d staging --skip-push --version="staging-mcp-$IMAGE_TAG"
kamal redeploy -c config/deploy-worker.yml -d staging --skip-push --version="staging-worker-$IMAGE_TAG"
kamal setup -c config/deploy-frontend.yml -d staging --version="staging-$IMAGE_TAG"
)
```

Subsequent deploys:

```bash
(
set -e
kamal build push -d staging --version="staging-api-$IMAGE_TAG"
kamal build push -c config/deploy-mcp.yml -d staging --version="staging-mcp-$IMAGE_TAG"
kamal build push -c config/deploy-worker.yml -d staging --version="staging-worker-$IMAGE_TAG"
kamal deploy -c config/deploy-cube.yml -d staging --version="cube-$IMAGE_TAG"
ssh -T "scout@$SCOUT_EC2_IP" bash -s -- staging 600 < scripts/drain-workers.sh
kamal deploy -d staging --skip-push --version="staging-api-$IMAGE_TAG"
kamal deploy -c config/deploy-mcp.yml -d staging --skip-push --version="staging-mcp-$IMAGE_TAG"
kamal redeploy -c config/deploy-worker.yml -d staging --skip-push --version="staging-worker-$IMAGE_TAG"
kamal deploy -c config/deploy-frontend.yml -d staging --version="staging-$IMAGE_TAG"
)
```

Omitting `-d staging` deploys **production** — the base configs are the production
definition.

The frontend commands carry an explicit `--version`. Without it Kamal versions the
build as the bare git SHA and pushes it as `scout/frontend:<sha>` — the same tag
production uses — but with `nginx.staging-kamal.conf` baked in. A later production
`kamal rollback`, host reboot, or re-pull of that version would then serve a
frontend proxying to `scout-staging-web`, putting production traffic on the staging
API. Backend commands also need explicit role/destination versions: their code is
environment-agnostic, but Kamal's image service labels differ by role.

Use GitHub Actions for normal deployments. These manual frontend builds do not
provide the workflow's Sentry build inputs or source-map upload configuration.

Migrations run automatically against the staging database when the API container
starts. Logs: `kamal app logs -d staging`.

### Migration-safe backend handoff

Run only one deployment on the shared host at a time, including manual commands.
The current production and staging workflows share the `scout-deploy-host`
concurrency group with `cancel-in-progress: false` and `queue: max`. This
serializes both destinations without replacing the other destination's pending
run; GitHub supports up to 100 pending runs. Manual shell commands and workflows
dispatched from older branch revisions are outside this updated group: check
both destinations before starting those, and do not overlap them with Actions.
See [GitHub's concurrency queue contract](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

Backend images are built and pushed before drain, using Kamal's role-specific
service labels. Post-drain commands pull/validate those exact images without
rebuilding them. Remote pulls, boot or health checks can still fail; the
interrupted-handoff notice and recovery procedure remain necessary.
Each post-drain API/MCP/worker/frontend deploy step has a 10-minute timeout so
a stuck remote operation does not silently pause the handoff for the runner's
six-hour default. The API's own migration/readiness deadline remains 180 seconds.
The deployment job also has a 90-minute cap for setup/prebuild hangs while it
holds the shared queue. Production's preceding reusable CI job remains a
separate gate with its existing timeout; the deployment cap is not a workflow-wide
deadline. The readiness script requires an exact HTTP 200, not a redirect or
another curl-success status.
Step timeouts do not prove that a remote operation stopped; inspect the host
before retrying an interrupted handoff.
The workflow drains **all active old worker versions** for the selected
destination before starting the new API. It also validates active containers
across the `scout-worker` service before any signal and while waiting: missing
or unsupported role/destination labels block the handoff rather than producing
a misleading empty inventory. Valid workers belonging to the other destination
are observed only and never signalled or given drain receipts.
Procrastinate's first `SIGTERM` stops
claiming jobs and lets running jobs finish. The drain helper sends that signal
once per container/process start and waits up to 10 minutes for clean exit.
It does not force-kill workers, restart them, or modify queued jobs. Jobs deferred
during the handoff wait until the new worker starts; interactive background work
can therefore pause for the duration of the rollout.

If a workflow fails or is cancelled after the drain starts and before the new
worker deploy succeeds, it emits an explicit error annotation and recovery steps
in the run summary. **Workers may remain stopped and queued jobs may remain
paused after that failed rollout.** Inspect worker state, in-flight jobs, and
pending receipts; resolve the failed gate and roll forward through the complete
destination-specific workflow once the worker/job state is safe. The notice
does not restart old workers or remove drain receipts. A runner that is abruptly
lost may not emit the notice, so inspect the handoff whenever a run ends there.

A timeout, cancellation or lost runner can leave a Kamal deployment lock behind.
Inspect it with `kamal lock status`, using the same `-c` config and `-d` destination
as the interrupted step (production omits `-d`). Kamal 2.12 scopes these locks by
service and destination; the Actions concurrency group is a separate lock.
Before using `kamal lock release` with those same arguments, confirm that no
deployment or remote operation is still active and that the lock is genuinely
stale. Never release a live or uncertain lock, and never automate lock release
in a failure handler. Releasing a verified stale lock does not establish that
worker drains or migrations succeeded; those gates must still pass on retry.

The API runs migrations and OAuth setup before starting uvicorn. Because this
service has no Kamal proxy, its container has an explicit loopback `/health/`
check with the configured allowed Host. The readiness check verifies the platform
database and queue; Kamal allows 180 seconds for migration/startup. Only after it
succeeds do MCP and the new worker deploy. Additive migrations must still be
compatible with old API/MCP readers and writers during this interval.
Docker can report `unhealthy` during that window; the pinned
[Kamal health poller](https://github.com/basecamp/kamal/blob/v2.12.0/lib/kamal/cli/healthcheck/poller.rb#L3-L29)
retries until the configured deadline rather than immediately failing on that
status. The 120-second Docker start period does not shorten Kamal's deadline.

If drain times out, **stop the deployment**. Existing jobs may still finish, but
queued jobs wait. Inspect the worker's logs and state, then retry the same
workflow after it finishes. A private pending receipt on the deployment host at
`.scout-worker-drains-v1/<destination>/<container-id>-<process-start>` is
created **before** the signal. Its root is anchored to the `scout` account's
passwd-defined home, independent of the caller's working directory; only that
deployment account may run the helper. It is separate from Kamal's own `.kamal`
directory and never changes that directory's permissions. Retries
revalidate these receipts even when a worker is no longer running and never send
a second signal. The helper removes only the exact receipt after confirming the
same process exited zero, was not OOM-killed, and no selected workers remain.

A failed, missing, restarted, or uncertain worker keeps its receipt and blocks
later retries. Inspect its jobs and process state before deciding on a manual
recovery; do not delete receipts, force-stop workers, prune failed containers, or
automatically restart an old publisher to bypass the gate. A receipt left before
an unconfirmed signal is deliberately observation-only and may require operator
recovery. Unexpected or malformed metadata also fails closed. The helper never
edits queue state, and successful cleanup does not assert that previously failed
jobs have been repaired.

An old `.kamal/scout-worker-drains-v1` path blocks the new helper before any
signal. Inspect its receipt/container/job state and explicitly reconcile or
migrate verified pending records into the new private root; never silently
abandon them, loosen permissions, or blindly remove a blocker. The helper does
not automatically migrate metadata through a potentially writable ancestor.

Worker startup uses `kamal redeploy --skip-push` after API/MCP have provisioned
the host. Workers have no accessories or proxy to bootstrap. In the pinned
Kamal 2.12.0 implementation, `redeploy` calls `app:boot`, which uploads this
worker role's current secret file with mode `0600` before starting a container
with fresh clear environment values (including `SENTRY_RELEASE`). It does not
reuse a stale worker environment or depend on API/MCP to update it. Kamal 2 has
no `kamal env push` command; do not add the old Kamal 1 command to this sequence.
See [the pinned redeploy entry point](https://github.com/basecamp/kamal/blob/v2.12.0/lib/kamal/cli/main.rb#L50-L75)
and [per-role boot environment upload](https://github.com/basecamp/kamal/blob/v2.12.0/lib/kamal/cli/app/boot.rb#L42-L55).

Unlike `deploy`,
this omits Kamal 2.12's **service-wide** pruning, which could erase a stopped
worker referenced by the other destination's pending receipts. Instead, the
pre-deploy disk guard (`scripts/host-disk-guard.sh prune-workers`) removes stopped
workers beyond the newest three per destination, and only when **no** receipt
exists in either destination and no legacy receipt path is present; otherwise it
leaves every stopped worker in place and warns (see [Host disk full](#host-disk-full)).
Any other worker cleanup is deliberate and destination-aware: do it only
after checking receipts and worker/job state in **both** environments. Do not
use service-wide worker `kamal prune`, or remove a receipt-referenced container,
to work around a blocked drain. ECR lifecycle policies are unchanged.

Once a provenance-aware worker has published `view_sources`, do not roll back or
restart a worker version that predates that field: an old publisher can change
physical views without updating their recorded source identities. Prefer rolling
forward, or choose a rollback version with the same publication contract and use
the full drain/migrate/health sequence. Keep the additive database column in
place; a bare `kamal rollback` of old workers is not a safe rollback procedure.

> Always `source config/staging.env` before staging commands — it points
> `DATABASE_URL` at the staging database. A plain `source .env.deploy` (prod)
> would deploy staging containers against the **production** database.
>
> Use a fresh terminal session with no staging overrides for **production**
> commands. Re-sourcing `.env.deploy` does not clear staging's exported database,
> Cube signing secret, or OAuth overrides. Clearing only `SCOUT_DB_NAME` is not
> sufficient to make a reused staging session safe for production.

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

Start in a fresh terminal session with no staging environment overrides; do not
reuse the session used for staging commands above.

```bash
# 1. Generate .env.deploy from CloudFormation outputs
./scripts/fetch-deploy-env.sh        # use -q/--quiet to suppress output
source .env.deploy
export IMAGE_TAG=$(git rev-parse HEAD)
```

Choose **one** fail-fast sequence below; do not combine first-time setup and
subsequent deployment in the same invocation.

First-time setup:

```bash
(
set -e
kamal build push --version="production-api-$IMAGE_TAG"
kamal build push -c config/deploy-mcp.yml --version="production-mcp-$IMAGE_TAG"
kamal build push -c config/deploy-worker.yml --version="production-worker-$IMAGE_TAG"
kamal setup -c config/deploy-cube.yml --version="cube-$IMAGE_TAG"
ssh -T "scout@$SCOUT_EC2_IP" bash -s -- production 600 < scripts/drain-workers.sh
kamal setup --skip-push --version="production-api-$IMAGE_TAG"
kamal setup -c config/deploy-mcp.yml --skip-push --version="production-mcp-$IMAGE_TAG"
kamal redeploy -c config/deploy-worker.yml --skip-push --version="production-worker-$IMAGE_TAG"
kamal setup -c config/deploy-frontend.yml --version="$IMAGE_TAG"
)
```

Subsequent deploys:

```bash
(
set -e
kamal build push --version="production-api-$IMAGE_TAG"
kamal build push -c config/deploy-mcp.yml --version="production-mcp-$IMAGE_TAG"
kamal build push -c config/deploy-worker.yml --version="production-worker-$IMAGE_TAG"
kamal deploy -c config/deploy-cube.yml --version="cube-$IMAGE_TAG"
ssh -T "scout@$SCOUT_EC2_IP" bash -s -- production 600 < scripts/drain-workers.sh
kamal deploy --skip-push --version="production-api-$IMAGE_TAG"
kamal deploy -c config/deploy-mcp.yml --skip-push --version="production-mcp-$IMAGE_TAG"
kamal redeploy -c config/deploy-worker.yml --skip-push --version="production-worker-$IMAGE_TAG"
kamal deploy -c config/deploy-frontend.yml --version="$IMAGE_TAG"
)
```

Frontend/Cube-only changes may deploy separately. Backend/model changes must use
the full migration-safe handoff above, including the worker drain.

```bash
(
set -e
kamal deploy -c config/deploy-cube.yml --version="cube-$IMAGE_TAG"
kamal deploy -c config/deploy-frontend.yml --version="$IMAGE_TAG"
)
```

Prefer the production GitHub Actions workflow for routine deploys, including the
frontend's required Sentry build configuration. For compatible rollback, use the
exact stored version for the selected service and destination and the publisher
restrictions above. Existing unqualified versions
are not renamed. ECR policies are unchanged; worker host retention follows the
receipt-aware procedure above rather than automatic service-wide pruning.

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
2. Redeploy the affected service(s). For API/MCP/worker changes, use the full
   migration-safe handoff above, including the graceful worker drain; do not
   bypass it for a logging-only rebuild.
3. Containers restart under the `json-file` driver; `kamal app logs` and
   `docker logs` work again immediately.

Existing CloudWatch log groups (`/scout/api`, `/scout/worker`, etc.) and all
historical streams are preserved with their 30-day retention — no data is lost.

### Host disk full

Production and staging share one host (a 50 GB root volume, per `infra/scout-stack.yml`), and every deploy pulls several ~1 GB images.
Stopped Kamal rollback containers pin their images, so the disk fills if pruning
stops. In September 2026 it did: the first pull of every deploy failed with
`no space left on device`, Kamal's end-of-deploy prune therefore never ran, and
nothing reached production for six days before anyone noticed.

**Symptoms**

- A deploy step (usually `Deploy Cube`, the first to pull) fails with
  `no space left on device`, or `Check host disk space` fails with
  `Host disk nearly full`.
- SSM Run Command reports failure with empty output.
- Session Manager refuses to connect with `Plugin with name Standard_Stream not found`.
- An open GitHub issue labelled `deploy-failure`.

**Recovery.** SSM needs free disk to work, so use SSH as the deploy user:

```bash
ssh scout@<host>
df -h /var/lib/docker && docker system df
docker image prune -af      # removes only images no container (running or stopped) uses
```

Rerun the failed deploy from the Actions tab. If that is not enough, check
stopped containers (`docker ps -a --filter status=exited`). Remove old API, MCP,
Cube or frontend containers freely, but never a stopped worker named by a
pending drain receipt (see [Migration-safe backend handoff](#migration-safe-backend-handoff)).

**Prevention.** Both deploy workflows now run, after SSH setup and before any backend build or host pull:

1. `Free host disk space` — `kamal prune all` for API, MCP, Cube and frontend
   (the same service-wide prune `kamal deploy` runs on success), a worker image
   prune, and the receipt-guarded worker container prune described above. Every
   role keeps `retain_containers: 3`. A single prune failure is a warning; if all
   of them fail (for example a stale Kamal lock), the step fails.
2. `Check host disk space` — fails the deploy with a clear error when Docker's
   filesystem has less than `HOST_MIN_FREE_GB` (8 GB) free, and warns below twice that.

A failed production deploy on `main` opens (or comments on) a single GitHub issue
labelled `deploy-failure` and mentions whoever pushed; the next successful deploy
closes it.

## Infrastructure Changes

> ### ⚠️ `update-stack` can replace the EC2 instance, and it does not need your permission
>
> `EC2Instance.ImageId` **used to be**
> `{{resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id}}`
> — resolving to whatever Canonical published **most recently**, at every stack operation. When a new
> 24.04 image appeared since the last one (roughly monthly), the resolved AMI ID differed,
> CloudFormation saw `ImageId` change, and **replaced the instance — whatever else you changed.**
> It is now the explicit `EC2AmiId` parameter, so routine updates no longer replace the instance;
> pass `ParameterKey=EC2AmiId,UsePreviousValue=true`. Taking a newer image is now a deliberate act
> (and still replaces the instance).
>
> **A restart is also an outage.** On this EBS-backed instance, changing `UserData`
> [restarts the instance](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-ec2-instance.html#cfn-ec2-instance-userdata)
> while retaining its root volume; it does not replace the instance. `Replacement: False`
> in a change set therefore does **not** mean zero downtime. Updated user data
> [does not run on restart by default](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html),
> so these bootstrap additions provision future new instances; updating the stack alone
> does not apply them to the existing machine.
>
> This is what happened on **2026-07-06**: an `update-stack` applying long-unapplied changes
> replaced the instance and took the site down. Full detail is in the 2026-07-06 SES/invites
> incident handover, which is kept **outside this repo** — it carries the account ID, the Elastic
> IP and the instance ID, and this repository is public. Ask in `#scout` for it. Everything an
> operator needs in the moment is in this block.
>
> **What a replacement costs.** A new instance boots from the template with a fresh EBS root
> volume. The Elastic IP re-associates, but everything on disk is gone.
> `UserData` reinstalls `scout`'s `authorized_keys` and recreates the `scout_shared` /
> `scout_staging_shared` networks, so CI can reconnect and `kamal deploy` restores the containers
> — **that is the whole reason those lines exist; do not remove them.** Before they were added,
> recovery required a human with the EC2 keypair to copy the key across by hand.
>
> **Do not run this command unless:**
> 1. you know the current instance may be replaced or restarted and that is acceptable right now;
> 2. the co-located staging stack going down with it is acceptable;
> 3. you have the EC2 keypair to hand, in case `UserData` fails part-way; and
> 4. you are prepared to re-run `kamal deploy` for every destination afterwards.
>
> To find out **before** you commit, create a change set instead of updating directly and inspect
> the `Replacement` column for `EC2Instance`. `Conditional` needs further inspection;
> neither it nor `False` rules out the restart outage described above.
>
> **Three traps in doing this by hand.** The first two produce *empty output that reads as
> &ldquo;no replacement&rdquo;*: `EC2AmiId` has no default, so it must be satisfied on every
> update-type change set or the call is rejected outright; and `create-change-set` returns as soon
> as the set is `CREATE_PENDING`, so describing it immediately shows an empty `Changes` list. The
> `wait` is what makes the answer trustworthy — a genuine no-replacement result prints rows with
> `Replace: False`, never nothing.
>
> The third is worse, because its output *looks* trustworthy. `ChangeSetName` must be unique per
> stack, and this procedure never executes the set — a direct `update-stack` only marks a pending
> set `OBSOLETE`, it does not delete it. With a fixed name, the second preflight fails to create,
> then describes **the previous run's set**: a confident `Replace:` table computed from the old
> template. Hence the timestamped name below; reuse `$CS` in all three calls, and never hard-code
> `preflight`.
>
> ```bash
> CS=preflight-$(date +%s)
>
> aws cloudformation create-change-set \
>   --stack-name scout-production --change-set-name "$CS" \
>   --template-body file://infra/scout-stack.yml \
>   --capabilities CAPABILITY_NAMED_IAM \
>   --parameters ParameterKey=EC2KeyPairName,UsePreviousValue=true \
>                ParameterKey=EC2AmiId,UsePreviousValue=true \
>   --profile scout --region us-east-1 &&
> aws cloudformation wait change-set-create-complete \
>   --stack-name scout-production --change-set-name "$CS" \
>   --profile scout --region us-east-1
>
> aws cloudformation describe-change-set \
>   --stack-name scout-production --change-set-name "$CS" \
>   --query '{Status:Status,Exec:ExecutionStatus,Why:StatusReason,Changes:Changes[].ResourceChange.{Res:LogicalResourceId,Action:Action,Replace:Replacement}}' \
>   --profile scout --region us-east-1
>
> aws cloudformation delete-change-set \
>   --stack-name scout-production --change-set-name "$CS" \
>   --profile scout --region us-east-1
> ```
>
> `create` and `wait` are `&&`-chained so a rejected create cannot fall through to a misleading
> describe. `describe` runs unchained on purpose: when the waiter fails because the template
> produces *no* changes, `Status`/`Why` are what tell you that, and with a unique name a failed
> create makes `describe` error loudly rather than answer from stale state. Deleting the set at the
> end is hygiene, not correctness — the unique name already makes an abandoned run harmless.
>
> On the **first** preflight against a stack that predates `EC2AmiId`, `UsePreviousValue=true`
> cannot work — there is no previous value. Pass the running instance's AMI explicitly, using the
> `describe-instances` lookup below.
>

The CloudFormation stack is at `infra/scout-stack.yml`. To update:

```bash
aws cloudformation update-stack \
  --stack-name scout-production \
  --template-body file://infra/scout-stack.yml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=EC2KeyPairName,UsePreviousValue=true \
               ParameterKey=EC2AmiId,UsePreviousValue=true \
  --profile scout \
  --region us-east-1
```

`EC2AmiId` has no default, by design — see the parameter's own description in
`infra/scout-stack.yml`. **On the first update after this change, pass the AMI the running
instance is already on**, not the latest Ubuntu image, or you trigger the replacement the pin
exists to prevent:

```bash
aws ec2 describe-instances --profile scout --region us-east-1 \
  --filters Name=tag:Name,Values=scout-web Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].ImageId' --output text
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
