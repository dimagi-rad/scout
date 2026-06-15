# CI Integrity Implementation Plan (arch issue #233)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CI actually run the real-DB regression suites it currently skips, gate production deploys on those tests passing, and stop baking secrets into Docker image layers.

**Architecture:** CI today starts a Postgres service and sets the platform `DATABASE_*` pieces, so platform-DB tests run — but it never sets `MANAGED_DATABASE_URL`, so two whole real-DB test modules plus the materializer writer tests silently skip (arch findings `12#2`, `10#3`). Production deploys (`deploy.yml`, on push to `main`) don't depend on tests at all (`08#4`), and there's no `.dockerignore`, so `COPY . .` bakes `.env`/`.env.deploy` into image layers (`08#3`). The fix: extract the test job into a **reusable workflow** (`test.yml`, `on: workflow_call`) that sets `MANAGED_DATABASE_URL` and runs `makemigrations --check`; have both `ci.yml` (PRs) and `deploy.yml` (pre-deploy gate) call it; add a guard test so the skip can't silently return; and add a `.dockerignore`.

**Tech Stack:** GitHub Actions (reusable workflows, `workflow_call`, `workflow_run` not needed), Postgres 16 service container, `uv run pytest`, pytest-django, Kamal (deploy, unchanged), Docker.

---

## Context the implementer needs

- **Test settings:** `config/settings/test.py` builds `DATABASES["default"]` from individual env vars (`DATABASE_USER/PASSWORD/HOST/PORT`, `TEST_DATABASE_NAME`). `MANAGED_DATABASE_URL` comes from `config/settings/base.py:126` (`env("MANAGED_DATABASE_URL", default="")`). Setting the `MANAGED_DATABASE_URL` env var both (a) un-skips the gated modules and (b) configures `settings.MANAGED_DATABASE_URL`.
- **The two DBs are distinct in CI:** the Postgres service auto-creates database `scout_test` (`POSTGRES_DB`). pytest-django creates Django's test DB as `test_scout_test` (the `test_` prefix). So `scout_test` is free to use as the managed DB — no collision.
- **The `scout` service user is a superuser** of the container, so it can `CREATE SCHEMA` and `CREATE ROLE <schema>_ro` (which provisioning does). CI is ephemeral, so no stale roles/schemas between runs.
- **Currently-skipped tests** (the ones this plan turns on):
  - `tests/test_view_schema_builder.py` (module-level skipif, line 19)
  - `tests/test_ocs_materializer.py` (module-level skipif, line 21)
  - `tests/test_materializer.py` writer tests: `test_inserts_cases` (~1090), `test_inserts_forms` (~1141), `test_write_messages` (~1190), and siblings (~1292, ~1361) — inline `pytest.skip` when no `MANAGED_DATABASE_URL`/`DATABASE_URL`.
- **Risk to expect:** turning these on may surface *real* failures the skip was hiding. That is the point. Task 1 establishes the baseline locally so that a red CI after Task 3 is unambiguous. Any genuine test failure that reproduces a known finding gets filed against the relevant arch issue (e.g. schema-lifecycle → #237), not worked around here.

## File Structure

- **Create:** `.github/workflows/test.yml` — reusable test workflow (`on: workflow_call`): Postgres service + lint-independent backend test job, `MANAGED_DATABASE_URL` set, `makemigrations --check`, `uv run pytest`. One responsibility: "run the backend test gate."
- **Modify:** `.github/workflows/ci.yml` — replace the inline `test` job with a call to `test.yml`; keep `lint-python` / `lint-frontend`.
- **Modify:** `.github/workflows/deploy.yml` — add a `test` job that calls `test.yml`; make `deploy` `needs: test`.
- **Create:** `tests/test_ci_integrity.py` — guard test that fails in CI if `MANAGED_DATABASE_URL` is unset (prevents silent regression of `12#2`).
- **Create:** `.dockerignore` — exclude `.env*`, VCS, caches, `.venv`, `node_modules`, `.claude/` (worktrees!), `docs/` from the build context (`08#3`).

---

### Task 1: Establish the local baseline for the currently-skipped suites

**Files:** none (verification only — no commit).

- [ ] **Step 1: Start the local platform DB**

Run: `docker compose up -d platform-db`
Expected: `platform-db` container healthy. (Connection: `platform:devpassword@localhost:5432/agent_platform`.)

- [ ] **Step 2: Run the gated suites with MANAGED_DATABASE_URL set**

Run:
```bash
MANAGED_DATABASE_URL="postgresql://platform:devpassword@localhost:5432/agent_platform" \
  uv run pytest tests/test_view_schema_builder.py tests/test_ocs_materializer.py \
  tests/test_materializer.py -v
```
Expected: the previously-skipped tests now COLLECT and RUN (no "skipped" for the module-level ones). Record the pass/fail result.

- [ ] **Step 3: Triage any failures**

If all pass → baseline is green; proceed. If any fail: determine whether the failure is (a) a real bug the skip was hiding — open/annotate the matching arch issue (schema-lifecycle → #237, OCS → #245, identifier → #235) and note it in the PR description; do **not** edit the test to pass — or (b) a local-env artifact (e.g. leftover schema/role from a prior local run) — clean up with `DROP SCHEMA ... CASCADE` / `DROP ROLE ...` and re-run. CI is ephemeral so (b) won't recur there.

- [ ] **Step 4: Record the baseline in the PR description draft**

Write down the exact counts (`N passed, M skipped`) before/after setting `MANAGED_DATABASE_URL` so the reviewer can see what CI was hiding.

---

### Task 2: Add `.dockerignore` so secrets/cruft don't enter image layers

**Files:**
- Create: `.dockerignore`

- [ ] **Step 1: Confirm the problem**

Run: `ls -la .env .env.deploy 2>/dev/null; ls .dockerignore 2>/dev/null || echo "no .dockerignore"`
Expected: `.env`/`.env.deploy` exist; no `.dockerignore`. (`Dockerfile:29` is `COPY . .`.)

- [ ] **Step 2: Create `.dockerignore`**

Create `.dockerignore`:
```gitignore
# Secrets — never bake into image layers (Dockerfile does `COPY . .`)
.env
.env.*

# VCS / CI
.git
.gitignore
.github

# Agent worktrees & local tooling (huge — each worktree is a full repo + .venv)
.claude

# Python caches / local virtualenv
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/

# Node / frontend build artifacts (frontend source is still copied for Dockerfile.frontend)
node_modules/
frontend/node_modules/
frontend/dist/

# Docs (large; not needed in the runtime image)
docs/

# Browser automation
.playwright/
.playwright-cli/
```

- [ ] **Step 3: Verify the API image still builds and `.env` is absent**

Run:
```bash
docker build -t scout-api-dockerignore-test . \
  && docker run --rm scout-api-dockerignore-test sh -c 'ls -la /app/.env 2>/dev/null && echo "LEAK" || echo "no .env in image — good"'
```
Expected: build succeeds; prints `no .env in image — good`. (The Dockerfile's `collectstatic` runs with a placeholder secret, so the build needs no real env.)

- [ ] **Step 4: Clean up the test image**

Run: `docker rmi scout-api-dockerignore-test`
Expected: image removed.

- [ ] **Step 5: Commit**

```bash
git add .dockerignore
git commit -m "build: add .dockerignore so .env and worktrees don't enter image layers

Dockerfile does COPY . . with no .dockerignore, baking .env/.env.deploy into
image layers (arch finding 08#3). Excludes secrets, .git, .claude worktrees,
caches, node_modules, and docs from the build context."
```

---

### Task 3: Extract a reusable test workflow that sets MANAGED_DATABASE_URL + makemigrations check

**Files:**
- Create: `.github/workflows/test.yml`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Create the reusable test workflow**

Create `.github/workflows/test.yml`:
```yaml
name: Backend Test Gate

on:
  workflow_call:

jobs:
  test:
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
          --health-cmd="pg_isready -U scout -d scout_test"
          --health-interval=10s
          --health-timeout=5s
          --health-retries=5
    env:
      DATABASE_USER: scout
      DATABASE_PASSWORD: scout
      TEST_DATABASE_NAME: scout_test
      DATABASE_HOST: localhost
      DATABASE_PORT: "5432"
      # Managed/tenant-data DB for the real-DB suites. Points at the pre-created
      # `scout_test` database; Django's own test DB is `test_scout_test`, so they
      # don't collide. Without this, the real-DB suites silently skip (arch 12#2/10#3).
      MANAGED_DATABASE_URL: "postgresql://scout:scout@localhost:5432/scout_test"
      DJANGO_SECRET_KEY: test-secret-key-not-for-production
      DJANGO_SETTINGS_MODULE: config.settings.test
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6

      - name: Check for missing migrations
        run: uv run python manage.py makemigrations --check --dry-run

      - name: Run tests
        run: uv run pytest
```

- [ ] **Step 2: Point ci.yml at the reusable workflow**

In `.github/workflows/ci.yml`, replace the entire `test:` job (lines 38–69, the `test:` key through the final `run: uv run pytest`) with:
```yaml
  test:
    uses: ./.github/workflows/test.yml
```
Leave `lint-python` and `lint-frontend` unchanged. The final `ci.yml` `jobs:` block is `lint-python`, `lint-frontend`, and `test: { uses: ./.github/workflows/test.yml }`.

- [ ] **Step 3: Validate the workflow YAML locally**

Run:
```bash
python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ['.github/workflows/test.yml','.github/workflows/ci.yml']]; print('YAML OK')"
```
Expected: `YAML OK`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/test.yml .github/workflows/ci.yml
git commit -m "ci: run real-DB suites + makemigrations check via reusable workflow

CI never set MANAGED_DATABASE_URL, so test_view_schema_builder, test_ocs_materializer
and the materializer writer tests silently skipped under a green badge (arch 12#2,
10#3). Extract a reusable test.yml that sets MANAGED_DATABASE_URL (pointing at the
scout_test DB, distinct from Django's test_scout_test) and adds makemigrations --check
(08#4). ci.yml now calls it."
```

- [ ] **Step 5: Verify on a PR**

Push the branch and open/refresh the PR. In the CI run's test job log, confirm: the migration-check step runs; pytest output shows the previously-skipped modules now executing (skip count drops by the suites from Task 1). Expected: green if Task 1 was green; if red, it's surfacing a real finding — triage per Task 1 Step 3.

---

### Task 4: Add a guard test so the skip can't silently return

**Files:**
- Create: `tests/test_ci_integrity.py`

- [ ] **Step 1: Write the guard test**

Create `tests/test_ci_integrity.py`:
```python
"""Guards that keep CI honest about running the real-DB suites (arch issue #233)."""

import os

import pytest


@pytest.mark.skipif(not os.environ.get("CI"), reason="only enforced in CI")
def test_managed_database_url_set_in_ci():
    """MANAGED_DATABASE_URL must be set in CI.

    Without it, tests/test_view_schema_builder.py, tests/test_ocs_materializer.py and
    the materializer writer tests skip via their module-level skipif — a green badge
    over untested real-DB code (arch findings 12#2, 10#3). GitHub Actions sets CI=true,
    so this assertion runs there and is skipped locally.
    """
    assert os.environ.get("MANAGED_DATABASE_URL"), (
        "MANAGED_DATABASE_URL is unset in CI; the real-DB regression suites would "
        "silently skip. Set it in .github/workflows/test.yml."
    )
```

- [ ] **Step 2: Run it locally (should skip)**

Run: `uv run pytest tests/test_ci_integrity.py -v`
Expected: `1 skipped` (no `CI` env var locally).

- [ ] **Step 3: Run it with the CI guard simulated (should pass)**

Run: `CI=true MANAGED_DATABASE_URL="postgresql://x" uv run pytest tests/test_ci_integrity.py -v`
Expected: `1 passed`.

- [ ] **Step 4: Run it with CI set but MANAGED unset (should fail — proves the guard bites)**

Run: `CI=true MANAGED_DATABASE_URL="" uv run pytest tests/test_ci_integrity.py -v`
Expected: `1 failed` with the assertion message.

- [ ] **Step 5: Commit**

```bash
git add tests/test_ci_integrity.py
git commit -m "test: guard that MANAGED_DATABASE_URL is set in CI

Fails the build if CI stops setting MANAGED_DATABASE_URL, preventing silent
regression of the real-DB suite skip (arch 12#2)."
```

---

### Task 5: Gate production deploys on the test workflow

**Files:**
- Modify: `.github/workflows/deploy.yml`

- [ ] **Step 1: Add a test gate job and make deploy depend on it**

In `.github/workflows/deploy.yml`, inside `jobs:` (above the existing `deploy:` job), add:
```yaml
  test:
    uses: ./.github/workflows/test.yml
```
Then add `needs: test` to the `deploy` job, immediately under `deploy:`:
```yaml
  deploy:
    needs: test
    runs-on: ubuntu-latest
    env:
      IMAGE_TAG: ${{ github.sha }}
    steps:
      ...
```
Result: on push to `main` (and `workflow_dispatch`), the full backend test gate runs first; build/push/deploy steps only run if it passes.

- [ ] **Step 2: Validate the YAML**

Run:
```bash
python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/deploy.yml')); assert d['jobs']['deploy']['needs']=='test'; assert d['jobs']['test']['uses']=='./.github/workflows/test.yml'; print('deploy gate OK')"
```
Expected: `deploy gate OK`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci: gate production deploy on the backend test workflow

deploy.yml ran on push to main with no dependency on tests (arch 08#4). Add a
test job (reusing test.yml) and make deploy need it, so a red test gate blocks
the deploy."
```

- [ ] **Step 4: Verify the gate end-to-end**

After merge to `main` (or via `workflow_dispatch` on the branch), confirm in the Actions run that the `test` job runs and `deploy` shows as waiting on it / skipped-if-failed. Do **not** intentionally break a test on `main` to prove this; rely on the `needs` graph and the run view.

---

## Self-Review

**Spec coverage (issue #233 findings):**
- `12#2` / `10#3` (CI skips real-DB suites) → Task 3 (MANAGED_DATABASE_URL) + Task 4 (guard).
- `08#4` (deploys not gated on tests; no makemigrations check) → Task 5 (deploy gate) + Task 3 (`makemigrations --check`).
- `08#3` (no .dockerignore → .env in image layers) → Task 2.

**Out of scope (intentionally):** migration *ordering* by convention and labs-migration opt-in (mentioned in `08#4`) are deploy-sequencing concerns tracked separately; this plan adds the missing-migration check and the deploy gate, which is the testable core. The chat↔MCP contract test (issue #234, the other Wave 0 item) is a separate plan.

**Placeholder scan:** none — every step has concrete commands/content.

**Consistency:** the managed DB URL `postgresql://scout:scout@localhost:5432/scout_test` is used identically in `test.yml`; the guard test keys on `CI` (GitHub-provided) and `MANAGED_DATABASE_URL` (set in `test.yml`). `ci.yml` and `deploy.yml` both reference `./.github/workflows/test.yml`.

**Risk callout:** Task 1 is mandatory before Task 3's PR — if the now-running suites fail, that is a real finding to file, not a reason to revert the wiring.
