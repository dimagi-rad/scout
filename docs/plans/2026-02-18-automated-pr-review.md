# Automated PR Review Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add CI checks (ruff, eslint, pytest) and Claude-powered AI code review as GitHub Actions workflows.

**Architecture:** Two workflow files in `.github/workflows/`. `ci.yml` runs three parallel blocking jobs (Python lint, frontend lint, tests). `claude-review.yml` uses `anthropic/claude-code-action@v1` for advisory AI review on every PR and `@claude` mentions.

**Tech Stack:** GitHub Actions, uv, bun, ruff, eslint, pytest, anthropic/claude-code-action@v1

---

### Task 1: Create CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

**Step 1: Write the CI workflow file**

```yaml
name: CI

on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened]

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint-python:
    name: Lint Python
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6

      - name: Run ruff
        run: uv run ruff check .

  lint-frontend:
    name: Lint Frontend
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: oven-sh/setup-bun@v2

      - name: Install dependencies
        run: bun install
        working-directory: frontend

      - name: Run ESLint
        run: bun run lint
        working-directory: frontend

  test:
    name: Tests
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_DB: scout_test
          POSTGRES_USER: scout
          POSTGRES_PASSWORD: scout
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      DATABASE_URL: postgres://scout:scout@localhost:5432/scout_test
      DJANGO_SECRET_KEY: test-secret-key-not-for-production
      DB_CREDENTIAL_KEY: ZmVybmV0LXRlc3Qta2V5LW5vdC1mb3ItcHJvZHVjdGlvbgo=
      DJANGO_SETTINGS_MODULE: config.settings.test
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6

      - name: Run tests
        run: uv run pytest
```

**Step 2: Validate the YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"`
Expected: No output (valid YAML)

**Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add CI workflow with Python lint, frontend lint, and tests"
```

---

### Task 2: Create Claude review workflow

**Files:**
- Create: `.github/workflows/claude-review.yml`

**Step 1: Write the Claude review workflow file**

```yaml
name: Claude Code Review

on:
  pull_request:
    types: [opened, synchronize]
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  issues: write

concurrency:
  group: claude-review-${{ github.event.pull_request.number || github.event.issue.number }}
  cancel-in-progress: true

jobs:
  review:
    name: Claude Review
    if: |
      (github.event_name == 'pull_request') ||
      (github.event_name == 'issue_comment' && contains(github.event.comment.body, '@claude')) ||
      (github.event_name == 'pull_request_review_comment' && contains(github.event.comment.body, '@claude'))
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt: ${{ github.event_name == 'pull_request' && '/review' || '' }}
          claude_args: "--max-turns 5"
```

**Step 2: Validate the YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/claude-review.yml'))"`
Expected: No output (valid YAML)

**Step 3: Commit**

```bash
git add .github/workflows/claude-review.yml
git commit -m "ci: add Claude AI code review workflow"
```

---

### Task 3: Generate a valid Fernet key for CI test env

The `DB_CREDENTIAL_KEY` env var in `ci.yml` needs to be a valid Fernet key (base64-encoded 32-byte key). The placeholder in Task 1 is not valid.

**Step 1: Generate a real Fernet key**

Run: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

**Step 2: Update `ci.yml` with the generated key**

Replace the `DB_CREDENTIAL_KEY` value in `.github/workflows/ci.yml` with the generated key.

**Step 3: Amend the CI commit**

```bash
git add .github/workflows/ci.yml
git commit --amend --no-edit
```

---

### Task 4: Manual setup steps (document for user)

These steps must be done by a repo admin in GitHub:

1. **Install the Claude GitHub App** — visit https://github.com/apps/claude and install it on the scout repo
2. **Add `ANTHROPIC_API_KEY` secret** — go to repo Settings > Secrets and variables > Actions > New repository secret, name it `ANTHROPIC_API_KEY`, paste your key
3. **Optionally configure branch protection** — require the `Lint Python`, `Lint Frontend`, and `Tests` status checks to pass before merging
