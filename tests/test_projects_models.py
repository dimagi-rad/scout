"""Tests for projects app models."""

import pytest

from apps.workspaces.models import SchemaState, TenantSchema


@pytest.mark.django_db
def test_tenant_schema_belongs_to_tenant_not_membership(tenant, user):
    schema = TenantSchema.objects.create(
        tenant=tenant,
        schema_name="test_schema",
        state=SchemaState.ACTIVE,
    )
    assert schema.tenant == tenant
    # old attribute must not exist
    assert not hasattr(schema, "tenant_membership_id")
