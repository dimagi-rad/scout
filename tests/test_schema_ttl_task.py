"""Tests for schema TTL Celery tasks (Task 4.2)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.projects.models import SchemaState, TenantSchema


@pytest.fixture
def active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="ttl_test_schema",
        state=SchemaState.ACTIVE,
        last_accessed_at=timezone.now(),
    )


@pytest.mark.django_db
def test_expire_inactive_schemas_marks_stale_schema_for_teardown(active_schema):
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=25)
    active_schema.save(update_fields=["last_accessed_at"])

    # transaction.on_commit doesn't fire inside test transactions; invoke immediately
    with (
        patch("apps.projects.tasks.transaction.on_commit", side_effect=lambda cb: cb()),
        patch("apps.projects.tasks.teardown_schema.delay") as mock_delay,
    ):
        from apps.projects.tasks import expire_inactive_schemas

        expire_inactive_schemas()

    active_schema.refresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN
    mock_delay.assert_called_once_with(str(active_schema.id))


@pytest.mark.django_db
def test_active_schema_not_expired_if_recently_accessed(active_schema):
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=1)
    active_schema.save(update_fields=["last_accessed_at"])

    from apps.projects.tasks import expire_inactive_schemas

    expire_inactive_schemas()

    active_schema.refresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.django_db
def test_schema_with_null_last_accessed_is_not_expired(active_schema):
    """Schemas that have never been accessed (null) should not be auto-expired."""
    active_schema.last_accessed_at = None
    active_schema.save(update_fields=["last_accessed_at"])

    from apps.projects.tasks import expire_inactive_schemas

    expire_inactive_schemas()

    active_schema.refresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.django_db
def test_teardown_schema_marks_expired_on_success(active_schema):
    patch_target = "apps.projects.services.schema_manager.SchemaManager"
    with patch(patch_target) as MockManager:
        MockManager.return_value.teardown.return_value = None
        from apps.projects.tasks import teardown_schema

        teardown_schema(str(active_schema.id))

    active_schema.refresh_from_db()
    assert active_schema.state == SchemaState.EXPIRED


@pytest.mark.django_db
def test_teardown_schema_rolls_back_to_active_on_failure(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    active_schema.save(update_fields=["state"])

    patch_target = "apps.projects.services.schema_manager.SchemaManager"
    with patch(patch_target) as MockManager:
        MockManager.return_value.teardown.side_effect = RuntimeError("DB error")
        from apps.projects.tasks import teardown_schema

        with pytest.raises(RuntimeError):
            teardown_schema(str(active_schema.id))

    active_schema.refresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE
