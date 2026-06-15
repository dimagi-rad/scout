# Gap Round 1 — Infrastructure & Network Topology

Reviewer mandate: deep-read `infra/scout-stack.yml` end to end and cross-check the
Kamal/deploy/nginx/docker plane. Answer four concrete questions, flag drift between the
CloudFormation reality and assumptions written into existing findings.

Scope read deeply: `infra/scout-stack.yml` (all 476 lines), `config/deploy.yml`,
`config/deploy-mcp.yml`, `config/deploy-worker.yml`, `config/deploy-frontend.yml`,
`.kamal/secrets`, `.kamal/hooks/{pre-connect,pre-deploy}`, `scripts/resolve-database-url.sh`,
`scripts/fetch-deploy-env.sh`, `docker-compose.yml`, `frontend/nginx.prod.conf`,
`frontend/nginx.prod-kamal.conf`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-labs.yml`.

---

## Direct answers to the four mandate questions

### (1) Is MCP port 8100 actually unreachable from outside the stack? — YES, the compensating control is REAL at the perimeter (but it is *only* a perimeter control)

Three independent layers keep 8100 off the public internet on the **production** (Kamal/EC2)
stack:

1. **Security-group ingress does not include 8100.** `EC2SecurityGroup`
   (`infra/scout-stack.yml:96-119`) opens only `22`, `80`, `443` from `0.0.0.0/0`. There is
   no ingress rule for 8100 (or 8000). The EC2 instance attaches *only* this SG
   (`scout-stack.yml:195-196`).
2. **The MCP container never publishes 8100 to the host.** `config/deploy-mcp.yml:14-18`
   runs the MCP process with `proxy: false`, `network: scout_shared`,
   `network-alias: scout-mcp-web`, and no host port mapping. Kamal with `proxy:false` and no
   explicit publish does not bind the container port to the host; 8100 is reachable only on
   the user-defined Docker bridge `scout_shared`. (The doc comment at `deploy-mcp.yml:1-2,52`
   — "MCP is internal only, reached by API via Docker network" — matches the wiring.)
3. **nginx does not proxy to MCP.** Neither `frontend/nginx.prod-kamal.conf` (production) nor
   `frontend/nginx.prod.conf` (labs) has a `/mcp` location; they proxy only
   `/api`, `/admin`, `/accounts`, `/health`, `/static`, `/embed` to `scout-web:8000`.

The same applies to the API: `config/deploy.yml:14-18` also runs `proxy:false` on 8000 with no
publish, and 8000 is absent from SG ingress. The **only** internet-facing service is the
frontend container, reached through the Kamal proxy on 80/443 (`config/deploy-frontend.yml:40-48`).

**So the existing finding "MCP HTTP server has no caller authentication … isolation is network
topology only" is accurate, and its compensating control is genuine — but only against
internet callers.** The isolation is pure reachability with **zero authentication**: every
workload that shares `scout_shared` (the API container, the worker container, the frontend
nginx container, and any future or compromised container) can call `teardown_schema` / `query`
with no credential. The single-EC2-host model means all four services are containers on **one
flat Docker network on one box**; a server-side compromise of any of them (e.g., a pivot out of
the agent code-execution surface, or the nginx/frontend container) lands directly adjacent to
an unauthenticated MCP. There is no network segmentation between the public-facing frontend and
the MCP control plane. This is the residual the existing finding describes; I confirm it and add
that the host-level topology offers no second layer behind the SG.

### (2) What outbound egress can the loaders / MCP host reach? — UNBOUNDED. No egress restriction anywhere, and IMDSv1 is reachable.

- **No `SecurityGroupEgress` is defined on any of the three security groups**
  (`EC2SecurityGroup`, `RDSSecurityGroup`, `RedisSecurityGroup`). Confirmed by grep: the file
  contains zero `Egress` keys. AWS default for a security group with no egress rules is
  **allow all outbound to `0.0.0.0/0`, all ports, all protocols.** The EC2 host (and therefore
  every container on it, including the MCP/loaders and the worker) can reach any internet host,
  any port, plus the VPC-internal RDS/Redis, plus the link-local IMDS endpoint `169.254.169.254`.
- **IMDSv1 is enabled.** `EC2Instance` (`scout-stack.yml:188-232`) sets no `MetadataOptions` /
  `HttpTokens`, so the instance defaults to `HttpTokens: optional` (IMDSv1 allowed). A plain
  GET-based SSRF — which is exactly the shape of a loader that follows a server-supplied `next`
  URL — can read `http://169.254.169.254/latest/meta-data/iam/security-credentials/scout-ec2-role`
  and exfiltrate the instance-role credentials.

This is the **infrastructure half of the existing app-layer finding** "Loaders follow
server-supplied next URLs anywhere with session-pinned credentials (no host/scheme validation)."
The app finding is about missing validation in loader code; this finding is that there is **no
network-level compensating control** behind it: egress is wide open and IMDS is reachable, so a
malicious `next` URL is bounded only by what the host can route to — i.e., the entire internet,
the VPC, and the instance metadata service. See finding INFRA-1 for the credential-theft chain
and its (modest) blast radius.

### (3) How long do access logs containing share tokens and OAuth codes persist in CloudWatch? — 30 days.

All four log groups set `RetentionInDays: 30`: `ApiLogGroup` (`scout-stack.yml:365-369`),
`McpLogGroup` (371-375), `WorkerLogGroup` (377-381), `FrontendLogGroup` (383-387). Every Kamal
service ships container stdout/stderr to these groups via the `awslogs` Docker driver
(`config/deploy*.yml` `logging:` blocks). The nginx config declares **no custom `access_log`**,
so the official-nginx default applies (access log to stdout), which the `awslogs` driver
captures into `/scout/frontend`. The OAuth `?code=…` callback and `/api/chat/threads/shared/<token>/`
/ `/api/recipes/runs/shared/<token>/` request lines therefore land in CloudWatch and **persist
for 30 days**. This is the concrete exposure window behind the existing finding "Share tokens and
OAuth codes transit uvicorn/nginx access logs into CloudWatch." 30 days is not pathological in
isolation, but it is the dwell time for plaintext bearer secrets, and CloudWatch read access is
broader than DB access. (No log-group-level KMS `KmsKeyId` is set either, so they use the default
CloudWatch service key.)

### (4) Do IAM roles grant the app hosts anything beyond need? — The running host role is genuinely minimal; the CI deploy role is over-broad.

- **`EC2Role` / `scout-ec2-role` (the running app host) is least-privilege** and is the answer
  to "anything beyond need" for the app hosts: it has `AmazonSSMManagedInstanceCore`
  (managed, `scout-stack.yml:171`) plus an inline `scout-cloudwatch-logs` policy granting only
  `logs:CreateLogStream` / `PutLogEvents` / `DescribeLogStreams`, scoped to the four
  log-group ARNs (`173-186`). It has **no** `secretsmanager`, **no** S3, **no** RDS admin. App
  secrets are injected as container env by Kamal at deploy time (resolved by CI / the operator,
  not by the instance role), so a compromise of the running instance role yields no secrets and
  no datastore admin. This is good and is recorded in "what's fine."
  - One residual: `AmazonSSMManagedInstanceCore` enables SSM Session Manager shell to the box,
    which together with SSH open to `0.0.0.0/0` (port 22) gives two administrative entry paths.
    Minor; see INFRA-5.
- **`GitHubDeployRole` / `scout-github-deploy` (CI) is over-broad on secrets** — see INFRA-2.
  Its `secretsmanager:GetSecretValue` resources include account-wide prefix wildcards
  (`rds!*`, `SCOUT_*`, `COMMCARE_*`, `CONNECT_*`, `OCS_*`) rather than specific ARNs, and it adds
  `secretsmanager:BatchGetSecretValue` on `Resource: '*'` (`scout-stack.yml:438-451`). Its trust
  is correctly pinned to `repo:dimagi-rad/scout:ref:refs/heads/main` (`411-415`), so it is not
  internet-assumable, but within a CI run it can read more secrets than it needs.

---

## Findings

### INFRA-1 — Wide-open egress + IMDSv1 leaves the loader SSRF unbounded and exposes instance-role credentials
- **Status:** LATENT · **Impact:** security · **Confidence:** strong-inference · **Complexity:** accidental
- **Reachable via:** Any provider loader that follows a server-supplied `next` URL (existing
  app finding "Loaders follow server-supplied next URLs anywhere"), running in the MCP/worker
  container on the EC2 host.
- **Claim:** The three security groups define no `SecurityGroupEgress`, so the AWS default
  (allow all outbound to `0.0.0.0/0`) applies; the EC2 instance sets no `MetadataOptions`, so
  IMDSv1 is enabled. A loader that fetches an attacker-controlled URL can therefore reach the
  entire internet (data exfil to any host), the VPC-internal RDS/Redis, and
  `http://169.254.169.254/latest/meta-data/iam/security-credentials/scout-ec2-role` to steal the
  instance-role credentials. There is no network-level mitigation behind the missing app-layer
  URL validation.
- **Chain:**
  - `infra/scout-stack.yml:96-119` — `EC2SecurityGroup` has `SecurityGroupIngress` only; no
    `SecurityGroupEgress` key anywhere in the template → default allow-all egress.
  - `infra/scout-stack.yml:188-232` — `EC2Instance` declares no `MetadataOptions` →
    `HttpTokens: optional` → IMDSv1 reachable by a simple GET.
  - Consequence: SSRF reach = internet ∪ VPC ∪ IMDS; IMDS GET returns `scout-ec2-role` creds.
- **Blast radius (honest):** `scout-ec2-role` grants only SSM-core + CloudWatch-logs-write
  (INFRA answer 4), so stolen instance creds are low value (cannot read app secrets or the DB).
  The larger practical risk is unrestricted **data exfiltration egress** and reaching internal
  services, not privilege escalation. Severity is LATENT (requires the known loader SSRF to
  fire), not BROKEN-NOW.
- **Drift flagged:** TODO.md lists "loader network egress restriction" as an unchecked security
  item; the infra confirms it is genuinely absent (no egress SG, no IMDSv2 enforcement).
- **Essential vs accidental:** Accidental — an egress SG scoped to provider API CIDRs/443 and
  `MetadataOptions: HttpTokens=required` would bound it without code change.

### INFRA-2 — CI deploy role can read every RDS master password and any prefixed secret in the account
- **Status:** LATENT · **Impact:** security · **Confidence:** verified-by-trace · **Complexity:** accidental
- **Reachable via:** A workflow run on `dimagi-rad/scout@main` (or anyone who can push to
  `main`, since `deploy.yml` runs on every push to main and assumes this role via OIDC).
- **Claim:** `GitHubDeployRole`'s `secretsmanager:GetSecretValue` is scoped by account-wide
  prefix wildcards, not specific ARNs: `rds!*` matches **every RDS-managed master-password
  secret in the account** (not just `scout-db`), and `SCOUT_*` / `COMMCARE_*` / `CONNECT_*` /
  `OCS_*` match any same-prefixed secret regardless of owner. It additionally grants
  `secretsmanager:BatchGetSecretValue` on `Resource: '*'`. If the account hosts any other RDS
  instance or any secret sharing those prefixes, the Scout CI role can read them.
- **Chain:**
  - `infra/scout-stack.yml:438-447` — `GetSecretValue` Resource list includes
    `…:secret:rds!*` and four `…:secret:<PREFIX>_*` wildcards.
  - `infra/scout-stack.yml:448-451` — `BatchGetSecretValue` on `Resource: '*'`.
- **Mitigating nuance:** AWS requires `Resource: '*'` for `BatchGetSecretValue` (it is a
  list-style call), and the batch call still requires `GetSecretValue` per secret to return
  values — so the *effective* read scope is bounded by the `GetSecretValue` resource list above,
  not by the `*`. The real over-grant is the **account-wide prefix wildcards**, especially
  `rds!*`. Best practice: enumerate the exact `scout/*` and `scout-db` secret ARNs.
- **Essential vs accidental:** Accidental — tightening to specific ARNs is a one-line-per-secret
  change with no functional impact.

### INFRA-3 — One database, one master-superuser credential: platform and tenant/managed data are co-located and the app connects as the RDS master
- **Status:** LATENT · **Impact:** security · **Confidence:** verified-by-trace · **Complexity:** mixed
- **Reachable via:** Every Django request and every MCP query — this is the production DB wiring.
- **Claim:** The stack provisions exactly **one** database (`RDSInstance` →
  `DBName: agent_platform`, `scout-stack.yml:249-271`) with a single master user
  (`MasterUsername: platform`, line 261). `scripts/resolve-database-url.sh:37` emits
  `postgresql://platform:<pw>@<endpoint>:5432/agent_platform` as `DATABASE_URL`, and
  `.kamal/secrets:43` sets `MANAGED_DATABASE_URL=$DATABASE_URL`. So the platform plane (Django
  models: users, sessions, **encrypted TenantConnection credentials**, LangGraph checkpoints,
  procrastinate queue) and the managed/tenant data plane (`ws_*` tenant schemas, view schemas)
  live in the **same database** and are both accessed as the **RDS master (`rds_superuser`)
  account.** The agent SQL path's `SET ROLE <readonly>` + `SET search_path` (per cartography
  §1, `mcp_server/services/query.py`) is the *only* confinement, and it runs on top of a
  superuser connection that can `SET ROLE` back to the owner.
- **Chain:**
  - `infra/scout-stack.yml:249-271` — single RDS instance, single DB `agent_platform`,
    `MasterUsername: platform`.
  - `scripts/resolve-database-url.sh:34-37` — `DATABASE_URL` is built with user `platform`
    (the RDS master).
  - `.kamal/secrets:42-43` — `MANAGED_DATABASE_URL` is literally `$DATABASE_URL`.
- **Relation to existing findings:** This is the **infra root** under several already-reported
  app findings ("Transform assets execute … as the managed-DB superuser", "SQL validator gaps
  where SET ROLE is the only defense", "Cross-tenant metadata disclosure via unqualified
  pg_catalog reads"). Those describe the app-layer symptom; this records the config fact they
  all rest on — there is no separate least-privilege application role and no separate managed
  database. I flag it as drift: reviewers reasoning about "platform DB ↔ managed DB" as two
  planes (cartography §4) should know they are one RDS instance, one DB, one superuser
  credential in production.
- **Essential vs accidental:** Mixed. Co-locating on one RDS instance is a defensible
  cost/ops choice at this scale (essential-ish), but connecting the app as the RDS *master*
  rather than a dedicated non-superuser role is accidental and removes the natural backstop for
  every SET-ROLE bypass.

### INFRA-4 — The CloudFormation stack only describes production; labs runs on different, out-of-repo infra (drift between stack reality and review assumptions)
- **Status:** DEBT · **Impact:** velocity (security-relevant) · **Confidence:** verified-by-trace · **Complexity:** accidental
- **Claim:** `infra/scout-stack.yml` provisions the **production** stack only: Kamal on a single
  EC2 host behind a Kamal proxy, RDS, ElastiCache, ECR, serving `scout.dimagi.com`
  (`config/deploy*.yml`). The **labs** deployment is a completely different runtime governed by
  infra **not in this repo**: ECS Fargate in AWS account `858923557655`, cluster
  `labs-jj-cluster`, services `labs-jj-scout-{web,mcp,worker}`, with task networking
  `assignPublicIp=ENABLED` and an opaque `LABS_SECURITY_GROUP` / `LABS_SUBNET`
  (`.github/workflows/deploy-labs.yml:21-30,239`). `frontend/nginx.prod.conf` (the labs nginx,
  `/scout/` base path) even references CloudWatch group `/ecs/labs-jj-scout-web`.
- **Why it matters for this review:** Findings and assumptions derived from `scout-stack.yml`
  (e.g. "8100 unreachable", "30-day retention", "Redis provisioned but unused", "no CloudWatch
  alarms", "no egress restriction") are **production-stack facts** and may not hold on labs,
  whose SG/egress/IMDS/retention posture is invisible to this repo. Notably `assignPublicIp=ENABLED`
  means labs Fargate task ENIs receive public IPs — the labs MCP/API reachability story depends
  entirely on `LABS_SECURITY_GROUP`, which is not auditable here. Existing findings that name
  "the stack" should be read as production-only.
- **Essential vs accidental:** Accidental drift — two deployment models with no single source of
  truth for the labs network posture.

### INFRA-5 — RDS/Redis in public subnets; broad management surface (SSH 0.0.0.0/0 + SSM); Django admin internet-exposed
- **Status:** DEBT · **Impact:** security · **Confidence:** verified-by-trace · **Complexity:** accidental
- **Claim (bundle of low-severity posture items, all verified in template/config):**
  - **RDS and Redis sit in public subnets.** `DBSubnetGroup` (`scout-stack.yml:241-247`) and
    `RedisSubnetGroup` (275-281) both reference `PublicSubnetA`/`PublicSubnetB`, which have
    `MapPublicIpOnLaunch: true` and a `0.0.0.0/0` route to the IGW (`47-92`). They are not
    internet-reachable **today** only because `PubliclyAccessible: false` (line 268) and the
    SG `SourceSecurityGroupId` restriction (130, 145). A single SG edit adding a `CidrIp`
    rule would directly expose the datastores; best practice is isolated/private subnets so the
    network is a second backstop.
  - **Two administrative entry paths:** SSH 22 open to `0.0.0.0/0` (`104-106`, key-only) **and**
    SSM Session Manager via `AmazonSSMManagedInstanceCore` (`171`). Either is an admin shell to
    the single host running all four services.
  - **Django admin is internet-exposed:** `frontend/nginx.prod-kamal.conf:90-96` proxies
    `/admin/` to Django, so `https://scout.dimagi.com/admin/` is a public admin login surface
    (session-auth + HTTPS gated, but reachable).
- **Essential vs accidental:** Accidental — private DB subnets, IMDSv2, SSH-via-SSM-only, and an
  admin IP allow-list are all standard hardening with no functional cost.

---

## Cross-checks that confirm existing findings (NOT re-reported as new)

- **"No detection layer: zero CloudWatch alarms":** Confirmed at the infra level — the template
  contains zero `AWS::CloudWatch::Alarm`, zero `MetricFilter`, zero `SNS`. Only log groups exist.
- **"Deploys not gated on tests":** Confirmed — `.github/workflows/deploy.yml` triggers on
  `push: branches:[main]`, has no `needs:` on `ci.yml` and no test step; it builds and deploys
  unconditionally. (`deploy-labs.yml` is `workflow_dispatch`-only and also runs no tests.)
- **"No .dockerignore … bakes .env into the image":** Confirmed — no `.dockerignore` exists at
  repo root.
- **"Chat rate limiting … Redis is provisioned but unused":** Confirmed on the production stack —
  `RedisCluster` is provisioned (`scout-stack.yml:283-296`) and `RedisEndpoint` is exported and
  threaded through `fetch-deploy-env.sh` / `deploy.yml`, but **no `deploy*.yml` references
  `REDIS_URL`** (grep: zero hits in config/deploy*.yml). The cache stays per-process LocMem.
- **"Share tokens and OAuth codes transit … into CloudWatch":** Confirmed and quantified —
  30-day retention (answer 3 above).
- **"MCP HTTP server has no caller authentication … isolation is network topology only":**
  Confirmed; compensating perimeter control is real (answer 1), residual intra-host trust stands.

---

## What's fine (verified healthy)

- **Internet attack surface is correctly minimized.** Only the frontend is public (Kamal proxy
  80/443); API:8000 and MCP:8100 are `proxy:false`, unpublished, SG-blocked, and not
  nginx-proxied. (answer 1)
- **EC2 instance role is least-privilege.** SSM-core + CloudWatch-logs-write to the four named
  log groups only; no secrets, S3, or RDS admin on the running host. (answer 4)
- **OIDC trust is correctly scoped.** `GitHubDeployRole` is assumable only from
  `repo:dimagi-rad/scout:ref:refs/heads/main` with the right audience condition — no
  internet-assumable role.
- **RDS hardening basics are present:** `StorageEncrypted: true`, `PubliclyAccessible: false`,
  `ManageMasterUserPassword: true` (rotated, in Secrets Manager), `BackupRetentionPeriod: 7`,
  `DeletionPolicy: Snapshot`, autoscaling storage 20→100 GB.
- **ECR lifecycle policies** keep the last 10 images per repo (bounded registry growth).
- **Secrets are not in the instance role:** they are fetched at deploy time from Secrets Manager
  (`.kamal/secrets`, `resolve-secrets`/`resolve-database-url.sh`) and injected as container env,
  so the steady-state host has no standing secret-read capability.
- **RDS/Redis ingress is SG-source-restricted** to the EC2 SG (not CIDR-open), and the RDS
  master password is never written to the template (RDS-managed).
- **nginx security headers** (`nosniff`, `X-Frame-Options SAMEORIGIN`, Referrer-Policy,
  Permissions-Policy) are set and correctly re-declared per-location in
  `nginx.prod-kamal.conf`, with the embed CSP `frame-ancestors` driven by `EMBED_ALLOWED_ORIGINS`.

---

## Coverage log

**Deep-read (line-by-line):** `infra/scout-stack.yml`, `config/deploy.yml`,
`config/deploy-mcp.yml`, `config/deploy-worker.yml`, `config/deploy-frontend.yml`,
`.kamal/secrets`, `.kamal/hooks/pre-connect`, `.kamal/hooks/pre-deploy`,
`scripts/resolve-database-url.sh`, `scripts/fetch-deploy-env.sh`, `docker-compose.yml`,
`frontend/nginx.prod.conf`, `frontend/nginx.prod-kamal.conf`, `.github/workflows/deploy.yml`,
`.github/workflows/deploy-labs.yml`.

**Skimmed / grep-only:** `cartography.md` (for context), `arch-review-methodology.md`.

**NOT examined (in or adjacent to scope, drives later gap-filling):**
- `Dockerfile` and `Dockerfile.frontend` — image build, base image, entrypoint/migrate-on-start,
  envsubst for CSP. Did not open; relevant to the no-`.dockerignore` and migrate-ordering
  findings and to whether the frontend image actually sets `access_log`.
- `scripts/resolve-secrets.sh` (referenced by `.kamal/secrets:2` "All fetch logic lives in
  scripts/resolve-secrets.sh") — file not located/opened; relevant to whether AWS calls are
  cached safely and which secrets are pulled.
- `registry_password.sh` (referenced by every `deploy*.yml` `registry.password`) — not opened.
- `.github/workflows/ci.yml`, `claude.yml`, `docs.yml` — only `deploy*.yml` were in scope; did
  not verify what CI actually runs (the "deploys not gated on tests" claim rests on the absence
  of a `needs:` in `deploy.yml`, which I did verify).
- The **labs ECS infrastructure itself** (task definitions, `LABS_SECURITY_GROUP`,
  `LABS_SUBNET`, target groups, ALB, log retention) — not in this repo; explicitly flagged as a
  blind spot in INFRA-4.
- **Runtime verification:** I did not (and cannot from static files) confirm that the deployed
  EC2 SG/IMDS/retention state matches the template (drift between committed CloudFormation and
  the live stack is possible), nor that the container can actually route to 169.254.169.254 on
  the `scout_shared` user-defined bridge — INFRA-1's IMDS reach is strong-inference (Docker
  user-defined bridges normally permit link-local IMDS access via the host) rather than
  verified-by-trace.
- The MCP `auth.py` / `query.py` / loader `next`-URL code paths — I read the cartography
  summaries and the existing findings but did not re-open the source; INFRA-1's app-side entry
  point relies on the already-reported loader-SSRF finding being accurate.
