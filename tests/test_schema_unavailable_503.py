"""Tests for 503 response when workspace schema is unavailable (Task 4.3)."""

import pytest
from rest_framework.test import APIClient

from apps.workspaces.models import SchemaState, TenantSchema


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.mark.django_db
def test_data_dictionary_503_when_no_schema(auth_client, workspace):
    """No schema at all → 503."""
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/data-dictionary/")
    assert resp.status_code == 503
    assert resp.data["schema_status"] == "unavailable"


@pytest.mark.django_db
def test_data_dictionary_503_when_schema_expired(auth_client, workspace, tenant):
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name="expired_schema",
        state=SchemaState.EXPIRED,
    )
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/data-dictionary/")
    assert resp.status_code == 503
    assert resp.data["schema_status"] == "unavailable"


@pytest.mark.django_db
def test_data_dictionary_503_when_schema_provisioning(auth_client, workspace, tenant):
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name="provisioning_schema",
        state=SchemaState.PROVISIONING,
    )
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/data-dictionary/")
    assert resp.status_code == 503
    assert resp.data["schema_status"] == "provisioning"


@pytest.mark.django_db
def test_table_detail_503_when_no_schema(auth_client, workspace):
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/data-dictionary/tables/public.users/")
    assert resp.status_code == 503
    assert resp.data["schema_status"] == "unavailable"
