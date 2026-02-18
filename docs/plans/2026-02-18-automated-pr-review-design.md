# Automated PR Review Design

## Goal

Set up two GitHub Actions workflows for the Scout repo:
1. **CI checks** (blocking) — linting and tests must pass to merge
2. **AI code review** (advisory) — Claude reviews diffs and posts comments

## Workflow 1: `ci.yml`

**Trigger:** `pull_request` targeting `main` (opened, synchronize, reopened)

Three parallel jobs:

### `lint-python`
- Set up Python 3.11+ with uv
- Run `uv run ruff check .`

### `lint-frontend`
- Set up Node with bun
- `bun install` and `bun run lint` in `frontend/`

### `test`
- Set up Python 3.11+ with uv
- PostgreSQL 16 service container
- Run `uv run pytest`
- Env vars: `DATABASE_URL`, `DJANGO_SECRET_KEY`, `DB_CREDENTIAL_KEY` (test values, not real secrets)

## Workflow 2: `claude-review.yml`

**Trigger:**
- `pull_request` (opened, synchronize) — automatic review
- `issue_comment` + `pull_request_review_comment` (created) — `@claude` mentions

**Action:** `anthropic/claude-code-action@v1`

**Configuration:**
- `prompt: "/review"` for automatic PR reviews
- `claude_args: "--max-turns 5"` to cap API cost
- Uses repo's `CLAUDE.md` for project-specific review criteria
- Advisory only — does not block merge

**Secrets required:**
- `ANTHROPIC_API_KEY` — added to repo secrets manually

**Permissions:**
- `contents: read`
- `pull-requests: write`
- `issues: write`

## Decisions

- Direct Anthropic API (no Bedrock/Vertex)
- Official Claude GitHub App (no custom app)
- Review only, no auto-fix
- Advisory — posts comments but doesn't block merge
