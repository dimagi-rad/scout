# Scout — Testing Guide

## Test categories

| Category | Command | Requirements |
|----------|---------|--------------|
| Unit / integration | `uv run pytest` | Postgres test DB |
| Cube e2e (live) | `CUBE_E2E=1 uv run pytest tests/e2e -m cube_e2e` | Full stack running + seed data |
| Smoke (deployment) | `uv run pytest -m smoke` | Live Scout instance |

---

## 1. Unit and integration tests

These use a throwaway test database and do not require Cube or the managed DB.

```bash
# Start the test database (port 5432 or 5433 if 5432 is in use)
docker compose up platform-db

# Run all tests
uv run pytest

# Run a single file or test
uv run pytest tests/test_semantic_query.py
uv run pytest -k test_name
```

> **Port conflict:** if Postgres is already running on 5432, set
> `TEST_DATABASE_PORT=5433` in `.env` before running pytest.

---

## 2. Live Cube e2e tests

These tests exercise the **full stack** — real Cube REST + SQL API, real
managed PostgreSQL — and prove both the happy-path and the tenant-isolation
security boundary.

### 2a. Stand up the stack

```bash
# Option A — Docker Compose (recommended for CI / first run)
docker compose up platform-db cube

# Apply Django migrations
uv run python manage.py migrate

# Option B — local processes via honcho (dev iteration)
uv run honcho -f Procfile.dev start
# → Django dev server  :8000
# → Cube REST + SQL    :4000 / :15432
# → Vite               :5173
```

### 2b. Seed demo workspaces

```bash
uv run python manage.py seed_demo
```

This idempotent command creates **two** isolated workspaces in the dev DB:

| Workspace | Tenant external_id | Schema | approval_rate | muac_rate |
|-----------|--------------------|--------|---------------|-----------|
| Demo Workspace   | 10001 | t_10001 | 0.60 | 0.70 |
| Demo Workspace B | 10002 | t_10002 | 0.40 | 0.50 |

It also writes Cube model YAML to `cube/model/t_10001/visits.yml` and
`cube/model/t_10002/visits.yml` — Cube auto-compiles these on first query.

Add `--verify` to also run an end-to-end Cube query after seeding:

```bash
uv run python manage.py seed_demo --verify
```

### 2c. Run the live e2e suite

```bash
CUBE_E2E=1 DJANGO_SETTINGS_MODULE=config.settings.development \
    uv run pytest tests/e2e/test_semantic_layer_live.py -v -m cube_e2e
```

**Expected output (4 tests passing):**

```
tests/e2e/test_semantic_layer_live.py::TestCubePathSmoke::test_workspace_a_count_and_rates PASSED
tests/e2e/test_semantic_layer_live.py::TestTenantIsolation::test_workspace_a_isolation PASSED
tests/e2e/test_semantic_layer_live.py::TestTenantIsolation::test_workspace_b_isolation PASSED
tests/e2e/test_semantic_layer_live.py::TestTenantIsolation::test_workspaces_have_different_metrics PASSED
```

#### What the tests assert

- **TestCubePathSmoke** — Workspace A returns `count=50, approval_rate=0.60,
  muac_confirmation_rate=0.70` via the Cube SQL API.
- **TestTenantIsolation** — Workspace A and Workspace B each return their own
  distinct metrics (0.60/0.70 vs 0.40/0.50), proving that the JWT
  `securityContext → schema_name` binding correctly isolates each tenant's
  `stg_visits` table. Cross-tenant data leakage would cause the assertion on
  differing metrics to fail.

#### Skip-by-default (CI)

Without `CUBE_E2E=1`, all four tests skip automatically:

```bash
uv run pytest tests/e2e
# → 4 skipped in 0.07s
```

The marker `cube_e2e` is registered in `pyproject.toml`; the global
`addopts = "-m 'not smoke'"` filter does not suppress it — the `skipif` guard
on `CUBE_E2E` is what keeps these off by default.

---

## 3. LLM-level tests (eval + agent graph)

Auto-model generation and agent eval require an Anthropic API key:

```bash
# Ensure ANTHROPIC_API_KEY is set in .env, then:
uv run pytest tests/test_eval_runner.py
uv run pytest tests/test_cube_model_generator.py
```

The `run_eval` command (if present) also reads `ANTHROPIC_API_KEY` from `.env`.

---

## 4. Linting

```bash
uv run ruff check .     # Python lint (line-length=100)
uv run ruff format .    # Auto-format

cd frontend && bun run lint  # TypeScript / ESLint
```

---

## Environment variables

Key variables for testing (see `.env.example` for full list):

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Platform Postgres (Django ORM + test DB) |
| `MANAGED_DATABASE_URL` | Tenant-data Postgres (managed schemas) |
| `ANTHROPIC_API_KEY` | Required for LLM / eval tests |
| `DB_CREDENTIAL_KEY` | Fernet key for encrypting DB credentials |
| `CUBE_E2E` | Set to `1` to run live Cube e2e tests |
| `DJANGO_SETTINGS_MODULE` | Use `config.settings.development` for e2e tests |
