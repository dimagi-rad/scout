# CloudWatch container logs implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream stdout/stderr from the four Scout production containers (`scout`, `scout-mcp`, `scout-worker`, `scout-frontend`) into per-service CloudWatch log groups with 30-day retention.

**Architecture:** Pre-create four log groups + an inline CloudWatch Logs IAM policy in `infra/scout-stack.yml`. Each Kamal service config (`config/deploy*.yml`) gains a top-level `logging:` block that switches Docker's log driver to `awslogs` and points it at the appropriate group. Local dev (`docker-compose.yml`) and the kamal-proxy container are untouched.

**Tech Stack:** AWS CloudFormation, AWS IAM, Kamal (1.x), Docker `awslogs` log driver, AWS CLI v2.

**Spec:** `docs/superpowers/specs/2026-05-13-cloudwatch-container-logs-design.md`

---

## File map

- **Modify:** `infra/scout-stack.yml` — add four `AWS::Logs::LogGroup` resources and attach an inline `scout-cloudwatch-logs` policy to the existing `EC2Role`.
- **Modify:** `config/deploy.yml` — add root-level `logging:` block targeting `/scout/api`.
- **Modify:** `config/deploy-mcp.yml` — add root-level `logging:` block targeting `/scout/mcp`.
- **Modify:** `config/deploy-worker.yml` — add root-level `logging:` block targeting `/scout/worker`.
- **Modify:** `config/deploy-frontend.yml` — add root-level `logging:` block targeting `/scout/frontend`.
- **Modify:** `DEPLOYMENT.md` — add a "Logs" subsection.

No application code changes. No tests files (this is a deploy-config change; verification is operational and documented in Task 7).

---

## Task 1: Add CloudWatch log groups to CloudFormation

**Files:**
- Modify: `infra/scout-stack.yml` (insert four log group resources after the `ECRFrontend` resource and before the `# ── GitHub OIDC ──` section header, i.e. around current line 344)

- [ ] **Step 1: Open `infra/scout-stack.yml`, locate the divider comment `# ── GitHub OIDC ──` (currently line 345). Insert the following block immediately *before* that divider so it sits inside the `Resources:` map.**

```yaml
  # ── CloudWatch Log Groups ───────────────────────────────────────
  # Pre-created so Kamal containers (using the awslogs Docker driver
  # with awslogs-create-group=false) can write streams without needing
  # logs:CreateLogGroup. Retention is enforced here.

  ApiLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: /scout/api
      RetentionInDays: 30

  McpLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: /scout/mcp
      RetentionInDays: 30

  WorkerLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: /scout/worker
      RetentionInDays: 30

  FrontendLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: /scout/frontend
      RetentionInDays: 30
```

- [ ] **Step 2: Validate the template parses.**

Run:
```bash
aws cloudformation validate-template \
  --template-body file://infra/scout-stack.yml \
  --profile scout \
  --region us-east-1
```

Expected output: JSON containing `"Description": "Scout production infrastructure"` and no error. If `validate-template` reports a YAML parse or schema error, fix indentation/syntax until it validates. **Do not proceed past this step until the template validates.**

- [ ] **Step 3: Commit.**

```bash
git add infra/scout-stack.yml
git commit -m "infra: add CloudWatch log groups for Scout services"
```

---

## Task 2: Grant the EC2 role permission to write to those groups

**Files:**
- Modify: `infra/scout-stack.yml` — extend the existing `EC2Role` resource (currently lines 159–171) with an inline `Policies:` block.

- [ ] **Step 1: Replace the existing `EC2Role` block.**

Currently the resource looks like this (around lines 159–171):

```yaml
  EC2Role:
    Type: AWS::IAM::Role
    Properties:
      RoleName: scout-ec2-role
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: ec2.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

Replace it with:

```yaml
  EC2Role:
    Type: AWS::IAM::Role
    Properties:
      RoleName: scout-ec2-role
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: ec2.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
      Policies:
        - PolicyName: scout-cloudwatch-logs
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action:
                  - logs:CreateLogStream
                  - logs:PutLogEvents
                  - logs:DescribeLogStreams
                Resource:
                  - !Sub '${ApiLogGroup.Arn}:*'
                  - !Sub '${McpLogGroup.Arn}:*'
                  - !Sub '${WorkerLogGroup.Arn}:*'
                  - !Sub '${FrontendLogGroup.Arn}:*'
```

The `:*` suffix on each resource ARN scopes to all log streams within the group, which is what `logs:CreateLogStream` and `logs:PutLogEvents` operate on.

- [ ] **Step 2: Re-validate the template.**

Run:
```bash
aws cloudformation validate-template \
  --template-body file://infra/scout-stack.yml \
  --profile scout \
  --region us-east-1
```

Expected: clean parse, no errors.

- [ ] **Step 3: Commit.**

```bash
git add infra/scout-stack.yml
git commit -m "infra: grant EC2 role CloudWatch Logs write access to /scout/*"
```

---

## Task 3: Update the production stack

**Files:** none (this is an operational step — but it MUST happen before Tasks 4–6 are merged to main, because containers will fail to start if the log groups don't exist).

- [ ] **Step 1: Update the CloudFormation stack.**

Run:
```bash
aws cloudformation update-stack \
  --stack-name scout-production \
  --template-body file://infra/scout-stack.yml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=EC2KeyPairName,UsePreviousValue=true \
  --profile scout \
  --region us-east-1
```

Expected: a `StackId` is returned immediately. The update runs asynchronously.

- [ ] **Step 2: Wait for the update to complete.**

Run:
```bash
aws cloudformation wait stack-update-complete \
  --stack-name scout-production \
  --profile scout \
  --region us-east-1
```

Expected: command exits 0 (no output) when status reaches `UPDATE_COMPLETE`. If it fails, run `aws cloudformation describe-stack-events --stack-name scout-production --profile scout --region us-east-1 --max-items 30` to find the failure and fix the template before retrying.

- [ ] **Step 3: Confirm the four log groups exist with 30-day retention.**

Run:
```bash
aws logs describe-log-groups \
  --log-group-name-prefix /scout/ \
  --profile scout \
  --region us-east-1 \
  --query 'logGroups[].{name:logGroupName,retention:retentionInDays}' \
  --output table
```

Expected: a 4-row table containing `/scout/api`, `/scout/mcp`, `/scout/worker`, `/scout/frontend`, each with `retention: 30`.

- [ ] **Step 4: Confirm the EC2 role has the new policy attached.**

Run:
```bash
aws iam list-role-policies \
  --role-name scout-ec2-role \
  --profile scout \
  --region us-east-1
```

Expected: the response includes `"scout-cloudwatch-logs"` in `PolicyNames`.

If both confirmations pass, proceed to Task 4. **If either fails, stop and diagnose — do not push the Kamal changes yet, because they'll cause containers to fail to start.**

---

## Task 4: Wire the Procrastinate worker to awslogs (lowest blast radius)

**Files:**
- Modify: `config/deploy-worker.yml` — add a root-level `logging:` block.

The worker is rolled out first because it has no inbound traffic — a misconfiguration here won't break end-user requests.

- [ ] **Step 1: Open `config/deploy-worker.yml`.**

Current end of the file (around lines 41–44):

```yaml
ssh:
  user: scout

builder:
  arch: amd64

# No proxy — worker has no HTTP interface
```

Add the following block immediately *after* the `builder:` block and *before* the trailing `# No proxy — ...` comment:

```yaml
logging:
  driver: awslogs
  options:
    awslogs-region: us-east-1
    awslogs-group: /scout/worker
    awslogs-create-group: "false"
    tag: "{{.Name}}"
```

- [ ] **Step 2: Commit.**

```bash
git add config/deploy-worker.yml
git commit -m "deploy: stream worker stdout to CloudWatch /scout/worker"
```

- [ ] **Step 3: Redeploy just the worker.**

Run (locally, with `.env.deploy` sourced — see `DEPLOYMENT.md` for setup):

```bash
source .env.deploy
kamal deploy -c config/deploy-worker.yml
```

Expected: Kamal builds (or reuses) the image, pushes if needed, and recreates the worker container with the new log driver. The deploy output should not show any log-driver errors.

- [ ] **Step 4: Verify a CloudWatch stream appears and receives events.**

Run:
```bash
aws logs tail /scout/worker --since 5m --profile scout --region us-east-1
```

Expected: at least one line of output. Procrastinate logs its startup banner and "fetching jobs" loop on boot, so you should see output within 30 seconds of the container starting.

If `aws logs tail` returns no events after 2 minutes, run `kamal app logs -c config/deploy-worker.yml` and `aws logs describe-log-streams --log-group-name /scout/worker --profile scout --region us-east-1 --max-items 5` to diagnose. Common causes: typo in the log group name, IAM role not yet propagated (wait 30s and retry), or the container is crash-looping (check `kamal app details -c config/deploy-worker.yml`).

- [ ] **Step 5: Push the worker change to main (so CI is consistent with what's deployed).**

If branching: open the PR with just this commit, get it merged. If the team's workflow is direct-to-main, push the commit. **Do not move on to Task 5 until the worker is confirmed working in CloudWatch** — that's the canary.

---

## Task 5: Wire the MCP server to awslogs

**Files:**
- Modify: `config/deploy-mcp.yml` — add a root-level `logging:` block.

- [ ] **Step 1: Open `config/deploy-mcp.yml`.**

Current end of the file (around lines 38–45):

```yaml
ssh:
  user: scout

builder:
  arch: amd64

# No proxy — MCP is internal only, reached by API via Docker network
```

Add the following block immediately *after* the `builder:` block and *before* the trailing `# No proxy — ...` comment:

```yaml
logging:
  driver: awslogs
  options:
    awslogs-region: us-east-1
    awslogs-group: /scout/mcp
    awslogs-create-group: "false"
    tag: "{{.Name}}"
```

- [ ] **Step 2: Commit.**

```bash
git add config/deploy-mcp.yml
git commit -m "deploy: stream MCP server stdout to CloudWatch /scout/mcp"
```

- [ ] **Step 3: Redeploy the MCP service.**

Run:
```bash
source .env.deploy
kamal deploy -c config/deploy-mcp.yml
```

Expected: clean deploy, no log-driver errors in output.

- [ ] **Step 4: Verify streams in CloudWatch.**

Run:
```bash
aws logs tail /scout/mcp --since 5m --profile scout --region us-east-1
```

Expected: MCP server startup output (FastMCP banner, uvicorn listening on `0.0.0.0:8100`) within ~30 seconds.

- [ ] **Step 5: Push to main.**

---

## Task 6: Wire the API and frontend to awslogs

**Files:**
- Modify: `config/deploy.yml` — add a root-level `logging:` block (API).
- Modify: `config/deploy-frontend.yml` — add a root-level `logging:` block (nginx).

These are rolled out together because, after Tasks 4 and 5, they're the last two and the pattern is now well-exercised.

- [ ] **Step 1: Open `config/deploy.yml`.**

Current end of the file (around lines 55–62):

```yaml
ssh:
  user: scout

builder:
  arch: amd64

# No proxy — API is internal only, reached via nginx in frontend container
```

Add the following block immediately *after* the `builder:` block and *before* the trailing `# No proxy — ...` comment:

```yaml
logging:
  driver: awslogs
  options:
    awslogs-region: us-east-1
    awslogs-group: /scout/api
    awslogs-create-group: "false"
    tag: "{{.Name}}"
```

- [ ] **Step 2: Open `config/deploy-frontend.yml`.**

Current end of the file (around lines 40–47):

```yaml
proxy:
  host: scout.dimagi.com
  app_port: 3000
  ssl: true
  forward_headers: true
  healthcheck:
    interval: 5
    path: /
    timeout: 3
```

Add the following block at the end of the file (after the `proxy:` block — it's a top-level sibling of `proxy:`, `env:`, etc.):

```yaml

logging:
  driver: awslogs
  options:
    awslogs-region: us-east-1
    awslogs-group: /scout/frontend
    awslogs-create-group: "false"
    tag: "{{.Name}}"
```

(Note the leading blank line to keep readability — `proxy:` is the previous block.)

- [ ] **Step 3: Commit both changes together.**

```bash
git add config/deploy.yml config/deploy-frontend.yml
git commit -m "deploy: stream api + frontend stdout to CloudWatch"
```

- [ ] **Step 4: Redeploy the API.**

Run:
```bash
source .env.deploy
kamal deploy -c config/deploy.yml
```

Expected: clean deploy. The API has a `/health/` endpoint that Kamal-internal healthchecks will hit; if the new container can't write logs, it'll fail healthchecks and Kamal will roll back automatically.

- [ ] **Step 5: Verify API streams.**

Run:
```bash
aws logs tail /scout/api --since 5m --profile scout --region us-east-1
```

Expected: Django/uvicorn startup output, then access lines as requests come in.

- [ ] **Step 6: Redeploy the frontend.**

Run:
```bash
source .env.deploy
kamal deploy -c config/deploy-frontend.yml
```

Expected: clean deploy. This is the public-facing service; if it fails, Kamal's proxy healthcheck (`/`, interval 5s) will refuse to switch traffic over.

- [ ] **Step 7: Verify frontend streams.**

Run:
```bash
aws logs tail /scout/frontend --since 5m --profile scout --region us-east-1
```

Expected: nginx access lines (the official nginx image already symlinks `/var/log/nginx/access.log` → `/dev/stdout` and `error.log` → `/dev/stderr`, so no nginx config changes are needed).

- [ ] **Step 8: Push to main.**

---

## Task 7: Document the logs in DEPLOYMENT.md

**Files:**
- Modify: `DEPLOYMENT.md` — add a "Logs" subsection under "Useful Commands".

- [ ] **Step 1: Open `DEPLOYMENT.md`. Locate the `## Useful Commands` section (currently around line 161).**

The section currently starts:

```markdown
## Useful Commands

```bash
# View logs
kamal app logs                    # API logs
kamal app logs -c config/deploy-mcp.yml  # MCP logs
```

Replace the `# View logs` block (the three lines including the `kamal app logs` examples) with:

```markdown
# View logs (CloudWatch)
#
# Container stdout/stderr ships to CloudWatch Logs in us-east-1. There is
# one log group per service; each container instance is its own stream.
#
# Log groups: /scout/api, /scout/mcp, /scout/worker, /scout/frontend
#
# Tail live:
aws logs tail /scout/api --follow --profile scout --region us-east-1

# Last 15 minutes:
aws logs tail /scout/api --since 15m --profile scout --region us-east-1
#
# CloudWatch Logs Insights queries: https://console.aws.amazon.com/cloudwatch/
#
# Note: `kamal app logs` shows nothing under the awslogs driver — Docker's
# `logs` command only works for the json-file/journald drivers. Use the
# `aws logs tail` commands above instead.
```

- [ ] **Step 2: Commit.**

```bash
git add DEPLOYMENT.md
git commit -m "docs: point operators at CloudWatch for container logs"
```

- [ ] **Step 3: Push to main.**

---

## Task 8: Rollback drill (documented procedure, not executed)

This task does **not** execute a rollback — it confirms the rollback path works on paper and documents it for incident response. No commits.

- [ ] **Step 1: Confirm the documented rollback steps.**

Rollback for a single service (example: the API):

1. Revert the `logging:` block in `config/deploy.yml` (delete it, or `git revert` the relevant commit).
2. Run `kamal deploy -c config/deploy.yml`.
3. The new container starts under Docker's default `json-file` driver. `kamal app logs` and `docker logs` work again.
4. CloudWatch log group `/scout/api` and its existing streams are preserved (30-day retention). No new events flow until the `logging:` block is restored.

- [ ] **Step 2: Confirm full-stack rollback if needed.**

Same procedure applied to all four `config/deploy*.yml` files. CFN changes from Tasks 1–2 can be left in place safely — pre-created log groups cost cents per month and the IAM policy is unused when the driver is reverted.

No commit. Task complete when the team has read these steps.

---

## Self-review

- **Spec coverage:**
  - Architecture (4 services, awslogs, per-service log groups, IAM, region us-east-1) → Tasks 1–6.
  - CloudFormation log groups with 30-day retention → Task 1.
  - IAM policy on `EC2Role` → Task 2.
  - Kamal `logging:` block in each `deploy*.yml` → Tasks 4, 5, 6.
  - DEPLOYMENT.md operator notes → Task 7.
  - Local dev unchanged → respected by omission (no edits to `docker-compose.yml`).
  - Kamal-proxy excluded → respected by omission.
  - Verification per-service → Tasks 4, 5, 6 (each ends with `aws logs tail`).
  - Rollback path → Task 8.
- **Placeholders:** none found.
- **Naming consistency:** log group names (`/scout/api`, `/scout/mcp`, `/scout/worker`, `/scout/frontend`), CFN resource names (`ApiLogGroup` / `McpLogGroup` / `WorkerLogGroup` / `FrontendLogGroup`), IAM policy name (`scout-cloudwatch-logs`), and Kamal `awslogs-group` values match across all tasks.
- **Order-of-operations:** CFN update (Task 3) gates the Kamal changes (Tasks 4–6), which is called out explicitly in Tasks 3 and 4.
