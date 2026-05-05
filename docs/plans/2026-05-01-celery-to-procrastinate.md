# Celery → Procrastinate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace Celery + Redis (broker/backend) + django-celery-beat with Procrastinate, using PostgreSQL as the task queue with native async tasks.

**Architecture:** Procrastinate uses Django's existing PostgreSQL DB as the job queue — no Redis broker needed. All 5 task functions become `async def`, using async ORM for DB calls and `asyncio.to_thread()` for sync operations (SchemaManager, run_pipeline). A standalone worker process runs via `manage.py procrastinate worker`.

**Tech Stack:** `procrastinate[django]>=0.28`, async Django ORM, `asyncio.to_thread()`, `unittest.mock.AsyncMock`

---

### Task 1: Swap Dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Edit pyproject.toml — remove Celery/Redis, add Procrastinate**

In the `dependencies` list, replace the three Caching & Task Queue lines:
```toml
# REMOVE these three:
"redis>=5.0",
"celery[redis]>=5.4",
"django-celery-beat>=2.6",

# ADD this one:
"procrastinate[django]>=0.28",
```

**Step 2: Sync dependencies**

```bash
uv sync
```

Expected: resolves without conflicts.

**Step 3: Verify import**

```bash
uv run python -c "import procrastinate; print(procrastinate.__version__)"
```

Expected: prints a version string (no ImportError).

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: replace celery/redis with procrastinate[django]"
```

---

### Task 2: Create Procrastinate App and Wire into Django

**Files:**
- Create: `config/procrastinate.py`
- Modify: `config/__init__.py`
- Modify: `config/settings/base.py`

**Step 1: Create `config/procrastinate.py`**

```python
"""Procrastinate app for background task processing."""

import procrastinate

app = procrastinate.App(connector=procrastinate.DjangoConnector())
```

**Step 2: Update `config/__init__.py`**

Replace the entire file:
```python
# Import procrastinate app so it's available when Django starts
from .procrastinate import app as procrastinate_app

__all__ = ("procrastinate_app",)
```

**Step 3: Update `INSTALLED_APPS` in `config/settings/base.py`**

Replace `"django_celery_beat",` with `"procrastinate.contrib.django",`:
```python
# Before:
"django_celery_beat",

# After:
"procrastinate.contrib.django",
```

**Step 4: Remove Celery settings and Redis cache from `config/settings/base.py`**

Remove the entire "Cache configuration" block (lines with `REDIS_URL`, `if REDIS_URL:`, `CACHES = {...}`) and replace with the simple fallback that was already in the else-branch:

```python
# Cache configuration
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# NOTE: LocMemCache is per-process — rate limiting won't work across
# multiple workers. Set up a shared cache for production deployments.
```

Remove the entire "Celery configuration" block (all `CELERY_*` settings and `CELERY_BEAT_SCHEDULE`). Keep `SCHEMA_TTL_HOURS`:

```python
SCHEMA_TTL_HOURS = 24  # schemas inactive longer than this are expired
```

**Step 5: Verify Django check passes**

```bash
uv run python manage.py check
```

Expected: `System check identified no issues (0 silenced).`

**Step 6: Commit**

```bash
git add config/procrastinate.py config/__init__.py config/settings/base.py
git commit -m "feat: wire procrastinate app, remove celery settings and redis cache"
```

---

### Task 3: Run Procrastinate Migrations + Seed Periodic Job

**Files:**
- Run: `manage.py migrate`
- Create: `apps/workspaces/migrations/XXXX_procrastinate_periodic_expire_schemas.py`

**Step 1: Apply Procrastinate's built-in migrations**

```bash
uv run python manage.py migrate
```

Expected: applies Procrastinate migrations creating `procrastinate_jobs`, `procrastinate_events`, `procrastinate_periodic_runs`, and related tables. Also removes `django_celery_beat` tables.

If you see "Table django_celery_beat_... does not exist" errors, check that `django_celery_beat` was removed from `INSTALLED_APPS` correctly.

**Step 2: Create a data migration to register the periodic job**

Procrastinate periodic tasks are defined in code with `@app.periodic(cron=...)` — this is added in Task 4. No data migration needed for periodic job registration; Procrastinate tracks runs automatically once the decorator is in place.

**Step 3: Commit**

```bash
# No new migration files to commit — procrastinate's migrations run via manage.py migrate
git commit --allow-empty -m "chore: procrastinate migrations applied (no new files)"
```

Actually skip this commit — there's nothing to commit.

---

### Task 4: Rewrite `tasks.py` — TDD (simpler tasks first)

**Files:**
- Modify: `tests/test_schema_ttl_task.py`
- Modify: `tests/test_rebuild_view_schema_task.py`
- Modify: `tests/test_refresh_task.py`
- Modify: `apps/workspaces/tasks.py`

#### 4a: Update teardown and TTL tests first

**Step 1: Rewrite `tests/test_schema_ttl_task.py`**

Key changes:
- All task calls become `await task(...)` (direct async call for tests, not `.defer_async()`)
- Tests that mock dispatch use `AsyncMock`
- Add `@pytest.mark.asyncio` and `@pytest.mark.django_db(transaction=True)` to all tests

```python
"""Tests for schema TTL tasks."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from django.utils import timezone

from apps.workspaces.models import SchemaState, TenantSchema


@pytest.fixture
def active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="ttl_test_schema",
        state=SchemaState.ACTIVE,
        last_accessed_at=timezone.now(),
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expire_inactive_schemas_marks_stale_schema_for_teardown(active_schema):
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=25)
    await active_schema.asave(update_fields=["last_accessed_at"])

    with patch(
        "apps.workspaces.tasks.teardown_schema.defer_async", new_callable=AsyncMock
    ) as mock_defer:
        from apps.workspaces.tasks import expire_inactive_schemas

        await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN
    mock_defer.assert_called_once_with(schema_id=str(active_schema.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_schema_not_expired_if_recently_accessed(active_schema):
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=1)
    await active_schema.asave(update_fields=["last_accessed_at"])

    from apps.workspaces.tasks import expire_inactive_schemas

    await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_schema_with_null_last_accessed_is_not_expired(active_schema):
    active_schema.last_accessed_at = None
    await active_schema.asave(update_fields=["last_accessed_at"])

    from apps.workspaces.tasks import expire_inactive_schemas

    await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_marks_expired_on_success(active_schema):
    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.return_value = None
        from apps.workspaces.tasks import teardown_schema

        await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.EXPIRED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_rolls_back_to_active_on_failure(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.side_effect = RuntimeError("DB error")
        from apps.workspaces.tasks import teardown_schema

        with pytest.raises(RuntimeError):
            await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE
```

**Step 2: Run TTL tests — confirm they fail**

```bash
uv run pytest tests/test_schema_ttl_task.py -v
```

Expected: `FAILED` with errors like `TypeError: object bool can't be used in 'await' expression` or `AttributeError: 'function' object has no attribute 'defer_async'` — confirms old sync tasks don't work in async tests.

**Step 3: Rewrite `tests/test_rebuild_view_schema_task.py`**

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="task@example.com", password="pass")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="task-domain", canonical_name="Task Domain"
    )


@pytest.fixture
def workspace(db, user, tenant):
    from apps.workspaces.models import TenantSchema

    ws = Workspace.objects.create(name="Task WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    TenantSchema.objects.create(tenant=tenant, schema_name="task_domain", state=SchemaState.ACTIVE)
    return ws


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_calls_build_view_schema(workspace):
    from apps.workspaces.tasks import rebuild_workspace_view_schema

    with patch("apps.workspaces.tasks.SchemaManager") as MockSM:
        mock_vs = MagicMock()
        mock_vs.schema_name = "ws_abc123"
        MockSM.return_value.build_view_schema.return_value = mock_vs

        result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))

    assert result["status"] == "active"
    mock_vs.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_fails_if_no_active_tenant_schema(workspace):
    from apps.workspaces.models import TenantSchema
    from apps.workspaces.tasks import rebuild_workspace_view_schema

    await TenantSchema.objects.filter(
        tenant__workspace_tenants__workspace=workspace
    ).aupdate(state=SchemaState.EXPIRED)

    result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_marks_failed_on_exception(workspace):
    from apps.workspaces.tasks import rebuild_workspace_view_schema

    with patch("apps.workspaces.tasks.SchemaManager") as MockSM:
        MockSM.return_value.build_view_schema.side_effect = Exception("boom")

        result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))

    assert "error" in result
    try:
        vs = await WorkspaceViewSchema.objects.aget(workspace=workspace)
        assert vs.state == SchemaState.FAILED
    except WorkspaceViewSchema.DoesNotExist:
        pass
```

**Step 4: Rewrite `tests/test_refresh_task.py`**

Key mock changes:
- `teardown_schema.apply_async` → `teardown_schema.defer_async` (AsyncMock)
- Task calls become `await refresh_tenant_schema(schema_id=..., membership_id=...)`
- `refresh_from_db()` → `arefresh_from_db()`

```python
"""Direct tests for the refresh_tenant_schema task."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.workspaces.models import SchemaState, TenantSchema


@pytest.fixture
def provisioning_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_domain_r12345678",
        state=SchemaState.PROVISIONING,
    )


@pytest.fixture
def old_active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_domain",
        state=SchemaState.ACTIVE,
    )


@pytest.fixture
def tenant_membership_obj(db, user, tenant):
    from apps.users.models import TenantMembership

    tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return tm


def _mock_conn():
    conn = MagicMock()
    conn.cursor.return_value = MagicMock()
    return conn


def _mock_registry(provider="commcare"):
    pipeline = MagicMock()
    pipeline.provider = provider
    pipeline.name = f"{provider}_sync"
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = MagicMock()
    return registry


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_schema_active_on_success(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry()),
        patch("apps.workspaces.tasks.run_pipeline"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.ACTIVE
    assert result["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_schedules_old_schema_teardown(
    provisioning_schema, old_active_schema, tenant_membership_obj
):
    """Old ACTIVE schemas are moved to TEARDOWN and a delayed task is scheduled."""
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry()),
        patch("apps.workspaces.tasks.run_pipeline"),
        patch(
            "apps.workspaces.tasks.teardown_schema.defer_async", new_callable=AsyncMock
        ) as mock_defer_async,
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await old_active_schema.arefresh_from_db()
    assert old_active_schema.state == SchemaState.TEARDOWN
    mock_defer_async.assert_called_once_with(
        schema_id=str(old_active_schema.id),
        schedule_in=timedelta(seconds=30 * 60),
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_schema_creation_error(
    provisioning_schema, tenant_membership_obj
):
    with patch(
        "apps.workspaces.services.schema_manager.get_managed_db_connection",
        side_effect=RuntimeError("Managed DB unreachable"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_no_credential(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch("apps.workspaces.tasks.resolve_credential", return_value=None),
        patch("apps.workspaces.services.schema_manager.SchemaManager.teardown"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_marks_failed_on_materialization_error(
    provisioning_schema, tenant_membership_obj
):
    with (
        patch(
            "apps.workspaces.services.schema_manager.get_managed_db_connection",
            return_value=_mock_conn(),
        ),
        patch(
            "apps.workspaces.tasks.resolve_credential",
            return_value={"type": "api_key", "value": "tok"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_mock_registry()),
        patch(
            "apps.workspaces.tasks.run_pipeline",
            side_effect=RuntimeError("Pipeline exploded"),
        ),
        patch("apps.workspaces.services.schema_manager.SchemaManager.teardown"),
    ):
        from apps.workspaces.tasks import refresh_tenant_schema

        result = await refresh_tenant_schema(
            schema_id=str(provisioning_schema.id),
            membership_id=str(tenant_membership_obj.id),
        )

    await provisioning_schema.arefresh_from_db()
    assert provisioning_schema.state == SchemaState.FAILED
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_refresh_task_returns_error_for_unknown_schema(tenant_membership_obj):
    from apps.workspaces.tasks import refresh_tenant_schema

    result = await refresh_tenant_schema(
        schema_id="00000000-0000-0000-0000-000000000000",
        membership_id=str(tenant_membership_obj.id),
    )
    assert "error" in result
```

**Step 5: Run all three test files — confirm they fail**

```bash
uv run pytest tests/test_schema_ttl_task.py tests/test_rebuild_view_schema_task.py tests/test_refresh_task.py -v
```

Expected: all `FAILED` — the old sync tasks can't be awaited.

**Step 6: Rewrite `apps/workspaces/tasks.py`**

```python
"""Background tasks for schema lifecycle management."""

import asyncio
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.users.models import TenantMembership
from apps.users.services.credential_resolver import resolve_credential
from apps.workspaces.models import SchemaState, TenantSchema, Workspace, WorkspaceViewSchema
from apps.workspaces.services.schema_manager import SchemaManager
from config.procrastinate import app
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.materializer import run_pipeline

logger = logging.getLogger(__name__)


@app.task
async def refresh_tenant_schema(schema_id: str, membership_id: str) -> dict:
    """Provision a new schema and run the materialization pipeline.

    On success: marks state=ACTIVE, schedules teardown of old active schemas.
    On failure: drops the new schema, marks state=FAILED.
    """
    try:
        new_schema = await TenantSchema.objects.select_related("tenant").aget(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.exception("refresh_tenant_schema: schema %s not found", schema_id)
        return {"error": "Schema not found"}

    try:
        membership = await TenantMembership.objects.select_related("tenant", "user").aget(
            id=membership_id
        )
    except TenantMembership.DoesNotExist:
        new_schema.state = SchemaState.FAILED
        await new_schema.asave(update_fields=["state"])
        return {"error": "Membership not found"}

    # Step 1: Create the physical schema in the managed database
    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.create_physical_schema, new_schema)
    except Exception:
        logger.exception("Failed to create schema '%s'", new_schema.schema_name)
        new_schema.state = SchemaState.FAILED
        await new_schema.asave(update_fields=["state"])
        return {"error": "Failed to create schema"}

    # Step 2: Resolve credential and run materialization pipeline
    credential = resolve_credential(membership)
    if credential is None:
        await _drop_schema_and_fail(new_schema)
        return {"error": "No credential available"}

    try:
        registry = get_registry()
        provider_pipeline_map = {p.provider: p.name for p in registry.list()}
        pipeline_name = provider_pipeline_map.get(membership.tenant.provider)
        if pipeline_name is None:
            await _drop_schema_and_fail(new_schema)
            return {"error": f"No pipeline configured for provider '{membership.tenant.provider}'"}
        pipeline_config = registry.get(pipeline_name)
        await asyncio.to_thread(run_pipeline, membership, credential, pipeline_config)
    except Exception:
        logger.exception("Materialization failed for schema '%s'", new_schema.schema_name)
        await _drop_schema_and_fail(new_schema)
        return {"error": "Materialization failed"}

    # Step 3: Mark new schema as active
    new_schema.state = SchemaState.ACTIVE
    await new_schema.asave(update_fields=["state"])

    # Step 4: Schedule teardown of previously active schemas with a delay to allow
    # in-flight queries against the old schema to complete before it is dropped.
    old_schemas = TenantSchema.objects.filter(
        tenant=new_schema.tenant,
        state=SchemaState.ACTIVE,
    ).exclude(id=new_schema.id)
    async for old_schema in old_schemas:
        old_schema.state = SchemaState.TEARDOWN
        await old_schema.asave(update_fields=["state"])
        await teardown_schema.defer_async(
            schema_id=str(old_schema.id),
            schedule_in=timedelta(seconds=30 * 60),
        )

    logger.info("Refresh complete: schema '%s' is now active", new_schema.schema_name)
    return {"status": "active", "schema_id": schema_id}


async def _drop_schema_and_fail(schema) -> None:
    """Drop the physical schema and mark the record as FAILED."""
    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown, schema)
    except Exception:
        logger.exception("Failed to drop schema '%s' during cleanup", schema.schema_name)
    schema.state = SchemaState.FAILED
    await schema.asave(update_fields=["state"])


@app.periodic(cron="*/30 * * * *")
@app.task
async def expire_inactive_schemas() -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    Handles both TenantSchema and WorkspaceViewSchema records.
    Schemas with null last_accessed_at are never auto-expired.
    """
    cutoff = timezone.now() - timedelta(hours=settings.SCHEMA_TTL_HOURS)

    # Expire stale tenant schemas
    async for schema in TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        schema.state = SchemaState.TEARDOWN
        await schema.asave(update_fields=["state"])
        await teardown_schema.defer_async(schema_id=str(schema.id))

    # Expire stale view schemas
    async for vs in WorkspaceViewSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        vs.state = SchemaState.TEARDOWN
        await vs.asave(update_fields=["state"])
        await teardown_view_schema_task.defer_async(view_schema_id=str(vs.id))


@app.task
async def rebuild_workspace_view_schema(workspace_id: str) -> dict:
    """Build (or rebuild) the UNION ALL view schema for a multi-tenant workspace.

    On success: marks WorkspaceViewSchema.state = ACTIVE.
    On failure: marks state = FAILED and returns an error dict.
    """
    try:
        workspace = await Workspace.objects.prefetch_related("tenants").aget(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.exception(
            "rebuild_workspace_view_schema: workspace %s not found", workspace_id
        )
        return {"error": "Workspace not found"}

    manager = SchemaManager()
    try:
        vs = await asyncio.to_thread(manager.build_view_schema, workspace)
    except Exception:
        logger.exception(
            "Failed to build view schema for workspace %s", workspace_id
        )
        return {"error": "Failed to build view schema"}

    logger.info(
        "View schema '%s' is now active for workspace %s",
        vs.schema_name,
        workspace_id,
    )
    return {"status": "active", "schema_name": vs.schema_name}


@app.task
async def teardown_view_schema_task(view_schema_id: str) -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema and mark EXPIRED."""
    try:
        vs = await WorkspaceViewSchema.objects.aget(id=view_schema_id)
    except WorkspaceViewSchema.DoesNotExist:
        logger.exception(
            "teardown_view_schema_task: view schema %s not found", view_schema_id
        )
        return

    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown_view_schema, vs)
    except Exception:
        logger.exception("Failed to drop view schema '%s'", vs.schema_name)
        vs.state = SchemaState.ACTIVE
        await vs.asave(update_fields=["state"])
        raise

    vs.state = SchemaState.EXPIRED
    await vs.asave(update_fields=["state"])


@app.task
async def teardown_schema(schema_id: str) -> None:
    """Drop a tenant schema in the managed database and mark it EXPIRED."""
    try:
        schema = await TenantSchema.objects.aget(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.exception("teardown_schema: schema %s not found", schema_id)
        return

    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown, schema)
    except Exception:
        schema.state = SchemaState.ACTIVE
        await schema.asave(update_fields=["state"])
        raise

    try:
        schema.state = SchemaState.EXPIRED
        await schema.asave(update_fields=["state"])
    except Exception:
        logger.exception(
            "teardown_schema: failed to mark schema %s EXPIRED after teardown", schema.id
        )
        raise
```

> **Note on `@app.periodic`:** Procrastinate requires the `@app.periodic` decorator to wrap a task. The stacking order is `@app.periodic(cron=...)` **above** `@app.task`. Check the installed Procrastinate version's docs if you get a decorator ordering error — some versions use `@app.task` + `periodic=True` parameter instead.

**Step 7: Run the three test files — confirm they pass**

```bash
uv run pytest tests/test_schema_ttl_task.py tests/test_rebuild_view_schema_task.py tests/test_refresh_task.py -v
```

Expected: all `PASSED`.

**Step 8: Commit**

```bash
git add apps/workspaces/tasks.py tests/test_schema_ttl_task.py tests/test_rebuild_view_schema_task.py tests/test_refresh_task.py
git commit -m "feat: convert workspace tasks to async procrastinate tasks"
```

---

### Task 5: Update Dispatch Call Sites and Their Tests

**Files:**
- Modify: `apps/workspaces/services/workspace_service.py`
- Modify: `apps/workspaces/api/views.py`
- Modify: `tests/test_refresh_endpoint.py`
- Modify: `tests/test_multitenant_smoke.py`
- Modify: `tests/test_view_schema_ttl.py`

#### 5a: Update `workspace_service.py`

`workspace_service.py` is sync and uses `transaction.atomic()`. Replace `delay_on_commit` with `defer` (sync). With Procrastinate, `defer()` inside `transaction.atomic()` enqueues the job as part of the transaction — it becomes visible to the worker only after commit. This gives the same safety guarantee as `delay_on_commit`.

In `add_workspace_tenant` (line 28):
```python
# Before:
rebuild_workspace_view_schema.delay_on_commit(str(workspace.id))

# After:
rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))
```

In `remove_workspace_tenant` (line 57):
```python
# Before:
rebuild_workspace_view_schema.delay_on_commit(str(workspace.id))

# After:
rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))
```

Also remove the inline `from apps.workspaces.tasks import rebuild_workspace_view_schema` from inside the function bodies and move it to module-level (top of file). The circular import issue is resolved because `tasks.py` no longer imports from `workspace_service.py`.

Wait — check whether moving the import to module level causes a circular import first:
```bash
uv run python -c "from apps.workspaces.services.workspace_service import add_workspace_tenant"
```

If it raises `ImportError` (circular), keep the imports inside the function bodies.

#### 5b: Update `views.py` dispatch (line ~362)

The DRF view is sync. Replace `delay_on_commit` with `defer`:

```python
# Before:
refresh_tenant_schema.delay_on_commit(schema_id, membership_id)

# After:
refresh_tenant_schema.defer(schema_id=schema_id, membership_id=membership_id)
```

Also update the import at the top of `views.py` if `refresh_tenant_schema` is imported there — change `from apps.workspaces.tasks import refresh_tenant_schema`.

#### 5c: Update `tests/test_refresh_endpoint.py`

The test that asserts the task is dispatched (currently mocks `.delay`) must be updated:

```python
@pytest.mark.django_db
def test_refresh_dispatches_task(manage_client, workspace, tenant_membership_for_user):
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as mock_defer:
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    schema_id = resp.data["schema_id"]
    mock_defer.assert_called_once_with(
        schema_id=schema_id,
        membership_id=str(tenant_membership_for_user.id),
    )
```

Remove the `patch("apps.workspaces.api.views.transaction.on_commit", ...)` line — it's no longer needed since `defer()` is direct.

The other tests that patched `transaction.on_commit` to prevent real task execution can also remove that patch — `defer()` inserts to the DB but the test transaction rolls back, so the job is never picked up by a worker.

#### 5d: Update `tests/test_multitenant_smoke.py`

```python
@pytest.mark.django_db(transaction=True)
def test_adding_tenant_dispatches_rebuild_task(api_client, setup):
    user, ws, t2 = setup

    with patch("apps.workspaces.tasks.rebuild_workspace_view_schema.defer") as mock_defer:
        api_client.force_login(user)
        resp = api_client.post(
            f"/api/workspaces/{ws.id}/tenants/",
            {"tenant_id": str(t2.id)},
            format="json",
        )

    assert resp.status_code == 202
    assert WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()
    mock_defer.assert_called_once_with(workspace_id=str(ws.id))
```

#### 5e: Update `tests/test_view_schema_ttl.py`

```python
def test_expire_inactive_schemas_also_expires_stale_view_schemas(workspace_with_view_schema):
    from apps.workspaces.tasks import expire_inactive_schemas
    import asyncio

    _ws, vs = workspace_with_view_schema
    vs.last_accessed_at = timezone.now() - timedelta(hours=25)
    vs.save()

    with patch(
        "apps.workspaces.tasks.teardown_view_schema_task.defer_async", new_callable=AsyncMock
    ) as mock_teardown:
        asyncio.run(expire_inactive_schemas())

    vs.refresh_from_db()
    assert vs.state == SchemaState.TEARDOWN
    mock_teardown.assert_called_once_with(view_schema_id=str(vs.id))
```

> **Note:** `test_view_schema_ttl.py` uses `transactional_db` fixtures (not asyncio markers). You can either add `@pytest.mark.asyncio` + `@pytest.mark.django_db(transaction=True)` to convert the tests to async (preferred), or use `asyncio.run()` as a bridge (quick fix). Prefer the async marker approach for consistency with the rest of the test suite.

**Step 1: Run all 3 updated test files — confirm they fail (old dispatch API)**

```bash
uv run pytest tests/test_refresh_endpoint.py tests/test_multitenant_smoke.py tests/test_view_schema_ttl.py -v
```

**Step 2: Apply all the call site and test changes described above**

**Step 3: Run all 6 task-related test files**

```bash
uv run pytest tests/test_refresh_task.py tests/test_schema_ttl_task.py tests/test_rebuild_view_schema_task.py tests/test_refresh_endpoint.py tests/test_multitenant_smoke.py tests/test_view_schema_ttl.py -v
```

Expected: all `PASSED`.

**Step 4: Run the full test suite**

```bash
uv run pytest
```

Expected: all existing tests pass (no regressions).

**Step 5: Commit**

```bash
git add apps/workspaces/services/workspace_service.py apps/workspaces/api/views.py \
        tests/test_refresh_endpoint.py tests/test_multitenant_smoke.py tests/test_view_schema_ttl.py
git commit -m "feat: update dispatch call sites to procrastinate defer"
```

---

### Task 6: Delete Celery Config

**Files:**
- Delete: `config/celery.py`

**Step 1: Delete the Celery config module**

```bash
rm config/celery.py
```

**Step 2: Verify Django starts cleanly**

```bash
uv run python manage.py check
```

Expected: no errors.

**Step 3: Run full test suite**

```bash
uv run pytest
```

Expected: all pass.

**Step 4: Commit**

```bash
git add -A
git commit -m "chore: delete config/celery.py"
```

---

### Task 7: Update Dev Tooling

**Files:**
- Modify: `docker-compose.yml`
- Modify: `Procfile.dev`
- Modify: `tasks.py`
- Modify: `CLAUDE.md`

**Step 1: Remove Redis from `docker-compose.yml`**

Remove the entire `redis:` service block and its volume. Also remove `redis` from any `depends_on` entries in other services.

Remove the `redis_data:` entry from the `volumes:` section at the bottom of the file.

**Step 2: Update `Procfile.dev`**

```
worker: watchfiles --filter python 'uv run python manage.py procrastinate worker' apps config mcp_server
```

**Step 3: Update `tasks.py`**

In `deps` task, remove `redis` from the docker-compose command:
```python
@task
def deps(c: Context) -> None:
    """Start Docker dependencies: platform-db and mcp-server."""
    c.run("docker compose up platform-db mcp-server", pty=True)
```

Add a new `worker` task:
```python
@task
def worker(c: Context) -> None:
    """Start the Procrastinate worker (standalone, without honcho)."""
    c.run("uv run python manage.py procrastinate worker", pty=True)
```

**Step 4: Update `CLAUDE.md`**

In the Commands section, update:
```markdown
# Backend
docker compose up platform-db mcp-server  # Start dependencies (Redis removed)
...
uv run python manage.py procrastinate worker  # Run background task worker
```

In the Architecture section, update the task queue description:
```markdown
- **Task Queue**: Procrastinate with PostgreSQL backend (no Redis required); async tasks in `apps/workspaces/tasks.py`
```

Remove any references to `CELERY_BROKER_URL`, `REDIS_URL`, or `django-celery-beat`.

**Step 5: Verify dev startup works**

```bash
uv run python manage.py check
```

```bash
# In one terminal:
uv run python manage.py procrastinate worker --dry-run  # check worker starts, list tasks
```

Expected: worker starts, lists the 5 registered tasks.

**Step 6: Run full test suite one final time**

```bash
uv run pytest
```

Expected: all pass.

**Step 7: Commit**

```bash
git add docker-compose.yml Procfile.dev tasks.py CLAUDE.md
git commit -m "chore: update dev tooling for procrastinate (remove redis, update worker command)"
```

---

## Summary of All Changes

| File | Action |
|---|---|
| `pyproject.toml` | Remove celery/redis/django-celery-beat, add procrastinate[django] |
| `config/celery.py` | **Delete** |
| `config/procrastinate.py` | **Create** |
| `config/__init__.py` | Import procrastinate app instead of celery |
| `config/settings/base.py` | Remove CELERY_*, REDIS_URL; add procrastinate to INSTALLED_APPS |
| `apps/workspaces/tasks.py` | Full rewrite: async def, @app.task, @app.periodic, asyncio.to_thread |
| `apps/workspaces/services/workspace_service.py` | `delay_on_commit` → `defer` |
| `apps/workspaces/api/views.py` | `delay_on_commit` → `defer` |
| `tests/test_refresh_task.py` | Async tests, AsyncMock for defer_async |
| `tests/test_schema_ttl_task.py` | Async tests, AsyncMock for defer_async |
| `tests/test_rebuild_view_schema_task.py` | Async tests |
| `tests/test_refresh_endpoint.py` | Mock defer instead of delay |
| `tests/test_multitenant_smoke.py` | Mock defer instead of delay_on_commit |
| `tests/test_view_schema_ttl.py` | Async tests, mock defer_async |
| `docker-compose.yml` | Remove redis service + volume |
| `Procfile.dev` | Update worker command |
| `tasks.py` | Update deps, add worker task |
| `CLAUDE.md` | Update docs |
