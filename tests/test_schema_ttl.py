"""Tests for TenantSchema last_accessed_at TTL tracking."""

from datetime import UTC, datetime

import freezegun
import pytest

from apps.projects.models import SchemaState, TenantSchema


@pytest.fixture
def tenant_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_schema",
        state=SchemaState.ACTIVE,
    )


@pytest.mark.django_db
def test_touch_updates_last_accessed_at(tenant_schema):
    original = tenant_schema.last_accessed_at
    with freezegun.freeze_time("2026-01-01 12:00:00"):
        tenant_schema.touch()
    tenant_schema.refresh_from_db()
    assert tenant_schema.last_accessed_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert tenant_schema.last_accessed_at != original


@pytest.mark.django_db
def test_saving_schema_without_touch_does_not_update_last_accessed_at(tenant_schema):
    original = tenant_schema.last_accessed_at
    tenant_schema.schema_name = tenant_schema.schema_name  # no-op save
    tenant_schema.save(update_fields=["schema_name"])
    tenant_schema.refresh_from_db()
    assert tenant_schema.last_accessed_at == original
