# Celery → Procrastinate Migration Design

**Date:** 2026-05-01
**Branch:** bdr/procrastinate

## Summary

Replace Celery + Redis (broker/backend) + django-celery-beat with Procrastinate, using PostgreSQL as the task queue. Remove Redis entirely. Convert all task functions to `async def`. Run the worker as a standalone process alongside the ASGI web server.

---

## 1. Architecture

**Before:** Django → Celery (via Redis broker) → Celery worker process. Beat schedules stored in PostgreSQL via `django-celery-beat`.

**After:** Django → Procrastinate (enqueues directly into PostgreSQL) → Procrastinate worker process. Periodic job schedules stored in PostgreSQL via Procrastinate's `PeriodicJob` model. Redis removed entirely.

The platform DB is already running — Procrastinate adds two tables (`procrastinate_jobs`, `procrastinate_periodic_runs`) via Django migrations. No new infrastructure required.

---

## 2. Task Definitions

All 5 tasks (`refresh_tenant_schema`, `expire_inactive_schemas`, `rebuild_workspace_view_schema`, `teardown_view_schema_task`, `teardown_schema`) become `async def` functions decorated with `@app.task` from Procrastinate.

The tasks are pure async DB and psycopg operations — no `asyncio.to_thread()` needed. (dbt is only invoked from the MCP server path, not from these tasks.)

The `debug_task` in `config/celery.py` is deleted.

**Note (out of scope):** `mcp_server/server.py:526` uses `sync_to_async(run_pipeline)` with the default `thread_sensitive=True`, which incorrectly serializes long-running dbt runs behind Django ORM calls. Should be changed to `thread_sensitive=False` in a separate PR.

---

## 3. Configuration & Infrastructure

**New file:** `config/procrastinate.py` — creates the `ProcrastinateApp` using `DjangoConnector`. Imported in `config/__init__.py` (replacing the Celery import).

**`config/settings/base.py` changes:**
- Remove all `CELERY_*` settings
- Remove `django_celery_beat` from `INSTALLED_APPS`, add `procrastinate`
- Remove `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`
- Django cache falls back to `LocMemCache` (already the configured fallback)

**`pyproject.toml`:** Remove `celery[redis]`, `django-celery-beat`, `redis`. Add `procrastinate[django]`.

**`docker-compose.yml`:** Remove `redis` service and volume.

**Migrations:** Procrastinate ships its own migrations. A new data migration seeds the `PeriodicJob` for `expire_inactive_schemas` (cron: `*/30 * * * *`).

**`tasks.py` (invoke):**
- `inv deps` → `docker compose up platform-db mcp-server` (drops `redis`)
- Add `inv worker` → `uv run python manage.py procrastinate worker`

**`Procfile.dev` worker line:**
```
worker: watchfiles --filter python 'uv run python manage.py procrastinate worker' apps config mcp_server
```

---

## 4. Dispatch Patterns

| Current | Procrastinate |
|---|---|
| `task.delay_on_commit(arg)` | `await task.defer_async_on_commit(arg)` |
| `task.apply_async((arg,), countdown=1800)` | `await task.defer_async(arg, schedule_in=datetime.timedelta(seconds=1800))` |

**Call sites to update (6 total):**
- `apps/workspaces/tasks.py:82` — `apply_async` with countdown
- `apps/workspaces/tasks.py:115` — `delay_on_commit` (teardown_schema)
- `apps/workspaces/tasks.py:125` — `delay_on_commit` (teardown_view_schema_task)
- `apps/workspaces/services/workspace_service.py:28` — `delay_on_commit` (rebuild)
- `apps/workspaces/services/workspace_service.py:57` — `delay_on_commit` (rebuild)
- `apps/workspaces/api/views.py:362` — `delay_on_commit` (refresh)

---

## 5. Testing

The 6 test files that mock Celery's `.delay()` / `.delay_on_commit()` need mocks updated to patch Procrastinate's `defer_async` / `defer_async_on_commit`.

Task functions become `async def`, so direct-call tests need `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)` — the same pattern already used for async view tests in this codebase. No new test infrastructure required.

**Affected test files:**
- `tests/test_refresh_task.py`
- `tests/test_schema_ttl_task.py`
- `tests/test_rebuild_view_schema_task.py`
- `tests/test_refresh_endpoint.py`
- `tests/test_multitenant_smoke.py`
- `tests/test_view_schema_ttl.py`
