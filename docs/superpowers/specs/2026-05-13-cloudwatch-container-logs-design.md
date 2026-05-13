# CloudWatch container logs — design

## Goal

Ship stdout/stderr from every Scout production container into AWS
CloudWatch Logs so operators have durable, searchable logs without
SSH'ing into the EC2 host.

Today every container in `config/deploy*.yml` logs to stdout/stderr
under Docker's default `json-file` driver. `kamal app logs` works, but
logs are ephemeral, host-local, and not queryable. After this change
the four application services stream into per-service CloudWatch log
groups with 30-day retention.

## Scope

In scope:

- Four application services: `scout` (Django API), `scout-mcp`,
  `scout-worker`, `scout-frontend` (nginx).
- Production deployment only — orchestrated by Kamal on the single
  EC2 instance defined in `infra/scout-stack.yml`.

Out of scope:

- Local development. `docker-compose.yml` is unchanged.
- The `kamal-proxy` container (excluded by request).
- Application-side structured/JSON logging changes — captures raw
  stdout/stderr as it is today. Can be revisited later.
- Metrics, traces, alarms. Logging only.

## Architecture

```
┌────────────── EC2 (scout-web) ──────────────┐         CloudWatch Logs (us-east-1)
│                                             │
│  scout-web         ──awslogs──▶             │  ──▶  /scout/api      (30d, stream per container)
│  scout-mcp-web     ──awslogs──▶  Docker     │  ──▶  /scout/mcp      (30d, stream per container)
│  scout-worker-web  ──awslogs──▶  daemon     │  ──▶  /scout/worker   (30d, stream per container)
│  scout-frontend    ──awslogs──▶             │  ──▶  /scout/frontend (30d, stream per container)
│                                             │
│  (kamal-proxy: unchanged, json-file driver) │
└─────────────────────────────────────────────┘
       ▲ uses scout-ec2-role (CloudWatch Logs perms via IMDS)
```

No new processes, no sidecars. Docker's built-in `awslogs` driver
streams each container's stdout/stderr directly to CloudWatch using
the EC2 instance role's IAM permissions.

## Components & changes

### 1. CloudFormation (`infra/scout-stack.yml`) — log groups

Four new `AWS::Logs::LogGroup` resources, each with
`RetentionInDays: 30`:

| Resource | Log group |
|---|---|
| `ApiLogGroup` | `/scout/api` |
| `McpLogGroup` | `/scout/mcp` |
| `WorkerLogGroup` | `/scout/worker` |
| `FrontendLogGroup` | `/scout/frontend` |

Pre-creating in IaC means retention is enforced from day one and the
EC2 role does not need `logs:CreateLogGroup`.

### 2. CloudFormation — IAM

Attach an inline policy named `scout-cloudwatch-logs` to the existing
`EC2Role` granting:

- `logs:CreateLogStream`
- `logs:PutLogEvents`
- `logs:DescribeLogStreams`

scoped to the four log group ARNs (each with `:*` suffix to cover all
streams). No wildcards.

### 3. Kamal configs — top-level `logging:` block

Each of `config/deploy.yml`, `config/deploy-mcp.yml`,
`config/deploy-worker.yml`, `config/deploy-frontend.yml` gets a
root-level `logging:` block. Example for the API:

```yaml
logging:
  driver: awslogs
  options:
    awslogs-region: us-east-1
    awslogs-group: /scout/api
    awslogs-create-group: "false"
    tag: "{{.Name}}"
```

The `awslogs-group` value differs per service (`/scout/api`,
`/scout/mcp`, `/scout/worker`, `/scout/frontend`). `tag: "{{.Name}}"`
makes each stream name match the Docker container name
(e.g. `scout-web-<commit-sha>`), which is how Kamal already names
containers.

### 4. `DEPLOYMENT.md` — operator documentation

Add a "Logs" subsection covering:

- Where logs live: CloudWatch console → log groups `/scout/api`,
  `/scout/mcp`, `/scout/worker`, `/scout/frontend`.
- Tail from CLI: `aws logs tail /scout/api --follow --profile scout`.
- Note: `kamal app logs` no longer shows historical output once the
  `awslogs` driver is in use (Docker's `logs` command returns nothing
  for log drivers other than `json-file`/`journald`). Redirect
  operators to CloudWatch.

### 5. Local development — no change

`docker-compose.yml` keeps Docker's default `json-file` driver. Only
the Kamal production configs add the `logging:` block.

## Data flow

1. App writes to **stdout/stderr** — Django via the existing
   `verbose` console handler in `config/settings/production.py`;
   nginx via the official image's stdout/stderr symlinks; MCP /
   worker via Python `logging` to console.
2. **Docker daemon** reads each container's stdout/stderr through the
   `awslogs` driver. No on-host `*-json.log` file is written under
   this driver.
3. Driver buffers lines and PUTs to **CloudWatch Logs** using the EC2
   instance role's credentials (resolved via IMDS). Target: log group
   `/scout/<service>`, stream name = container name from `tag`.
4. **CloudWatch** retains 30 days, then expires.
5. **Operators** read via CloudWatch console, `aws logs tail`, or
   CloudWatch Logs Insights queries.

Region is hard-coded to `us-east-1` in the driver options — explicit,
matches the stack.

## Failure modes

- **AWS API unreachable / throttled.** The default `awslogs` driver
  is blocking — a stalled CloudWatch endpoint can back-pressure
  container writes. We accept the default for now (low log volume,
  simple). If we observe blocked containers in practice, switch to
  `awslogs-mode: non-blocking` with a bounded `awslogs-max-buffer-size`,
  trading dropped logs for liveness. One-line follow-up change.
- **Log group missing.** With `awslogs-create-group: "false"`, a
  missing group makes the container fail to start with a clear
  Docker error. This is intentional: CFN is the source of truth for
  log groups, so the rollout order is "update CFN first, then deploy".
- **Early-exit crash logs.** Output emitted before the Docker daemon
  attaches stdout (a very brief entrypoint window) is not captured.
  Same limitation as today; acceptable.
- **IAM regression.** If the EC2 role loses CloudWatch permissions,
  affected containers fail to start with a Docker log-driver error
  message identifying the missing permission. Easy to diagnose.

## Testing & verification

No automated tests — this is a deployment configuration change with
no application code paths.

Verification is operational:

1. **CFN template validation** —
   `aws cloudformation validate-template --template-body file://infra/scout-stack.yml`.
2. **Update the stack** to create the four log groups and attach the
   IAM policy. Confirm groups exist with the expected retention.
3. **Deploy the worker first** (lowest blast radius — no inbound
   traffic). Confirm a stream appears in `/scout/worker` and
   `aws logs tail /scout/worker --since 5m` shows the worker's
   startup output.
4. **Roll forward** to `scout-mcp`, then `scout` (API), then
   `scout-frontend`, verifying each `/scout/<service>` log group
   receives events.
5. **Rollback drill** — verify reverting the `logging:` block in one
   service and redeploying restores the `json-file` driver (so
   `docker logs` and `kamal app logs` work again). Document this
   path in `DEPLOYMENT.md`.

Optional follow-up (not in scope): a CloudWatch Logs metric filter +
alarm for "no log events in 15 minutes" per service, to page on a
silent-failure regression where the driver is wired but not flowing.

## Rollout plan

Order matters because the Docker driver requires the log groups to
exist:

1. Merge CFN changes (new log groups + IAM policy). Update the
   `scout-production` stack via the existing
   `aws cloudformation update-stack` workflow.
2. Merge Kamal config changes. The push-to-main CI pipeline will
   redeploy all four services.
3. Verify each `/scout/*` log group receives streams.
4. Update `DEPLOYMENT.md` (can ship with step 2).

Rollback: revert the Kamal `logging:` blocks and redeploy. Log groups
in CFN can stay (no cost beyond a few KB of retention metadata) or
be removed in a follow-up.

## Trade-offs considered

**Approach chosen: Docker `awslogs` driver via Kamal `logging:` block.**
Native Docker, no extra processes, per-service streams, IaC-managed
groups.

Alternatives rejected:

- **CloudWatch agent on the EC2 host** — tails
  `/var/lib/docker/containers/*-json.log`. One config covers all
  current and future containers, but heavier setup, requires parsing
  Docker's JSON wrapper, and you lose per-service IAM scoping.
- **Fluent Bit sidecar** — most flexible (filtering, parsing, routing)
  but adds a deploy unit and configuration surface we do not need
  for four containers' worth of stdout/stderr.

**Retention chosen: 30 days.** Common default; balances cost
(~$0.03/GB/month CloudWatch Logs storage) against debugging windows.
Easy to extend later via the CFN property.

**Log group layout chosen: one group per service** (`/scout/api`,
`/scout/mcp`, `/scout/worker`, `/scout/frontend`). Enables per-service
retention, per-service IAM scoping, and direct CloudWatch Insights
queries without filtering. Rejected: a single `/scout` group with
service-in-stream-name (harder to scope) and per-environment groups
like `/scout/production/api` (premature — no staging environment yet).
