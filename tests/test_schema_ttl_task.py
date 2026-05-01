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
    """Schemas that have never been accessed (null) should not be auto-expired."""
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
