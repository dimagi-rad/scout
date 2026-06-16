# Testing

Every runnable test path in Scout.

## Backend (Python / pytest)

```bash
uv run pytest                 # full suite (smoke tests excluded by default)
uv run pytest tests/test_x.py # one file
uv run pytest -k name         # by name
```

The default `addopts` (`pyproject.toml`) include `-m 'not smoke'`, so smoke tests
are excluded from the normal run. CI runs `uv run pytest` via
`.github/workflows/test.yml`.

## Frontend unit tests (Vitest + Testing Library)

```bash
cd frontend
bun run test        # one-shot (used by CI)
bun run test:watch  # watch mode for local dev
```

Runs in jsdom. Config: `frontend/vitest.config.ts`; setup:
`frontend/src/test/setup.ts`. CI runs `bun run test` in the `lint-frontend` job
(`.github/workflows/ci.yml`).

## Smoke tests (post-deploy, live deployment)

HTTP checks against a running deployment. Excluded from the default backend run;
run explicitly:

```bash
uv run pytest tests/smoke/test_deployment.py \
    -v --override-ini="addopts=" -p no:django
# configure target via env or tests/smoke/.env:
#   SCOUT_FRONTEND_URL=... SCOUT_API_URL=...
#   SCOUT_TEST_EMAIL=... SCOUT_TEST_PASSWORD=...  (authenticated-flow tests)
```

In CI: run the **Smoke Tests (manual)** workflow
(`.github/workflows/smoke.yml`) via *Actions → Run workflow*, supplying the
target URLs. `test_connect_sync.py` additionally needs platform-DB access — see
`tests/smoke/conftest.py` and `tests/smoke/.env.example`.

## Frontend end-to-end (Playwright)

```bash
cd frontend
bun run test:e2e              # all projects
bun run test:e2e:widget       # widget-sdk project only
bun run test:e2e:integration  # embed-integration (auto-starts API + Vite)
```

Config: `frontend/playwright.config.ts`. Some projects require a live backend
and/or credentials. Fully gated, credentialed e2e in CI is a planned follow-up
(see #234).
