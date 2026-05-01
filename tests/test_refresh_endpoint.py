"""Tests for data refresh endpoint (Task 4.1)."""

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.workspaces.models import SchemaState, TenantSchema


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def manage_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def tenant_membership_for_user(db, user, tenant):
    from apps.users.models import TenantMembership

    # Use get_or_create since the workspace signal may have already created one
    tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return tm


@pytest.mark.django_db
def test_refresh_returns_202(manage_client, workspace, tenant_membership_for_user):
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer"):
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 202
    assert resp.data["status"] == "provisioning"
    assert "schema_id" in resp.data


@pytest.mark.django_db
def test_refresh_creates_provisioning_schema(
    manage_client, workspace, tenant, tenant_membership_for_user
):
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer"):
        manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert TenantSchema.objects.filter(tenant=tenant, state=SchemaState.PROVISIONING).exists()


@pytest.mark.django_db
def test_refresh_dispatches_task(manage_client, workspace, tenant_membership_for_user):
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as mock_defer:
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    schema_id = resp.data["schema_id"]
    mock_defer.assert_called_once_with(
        schema_id=schema_id,
        membership_id=str(tenant_membership_for_user.id),
    )


@pytest.mark.django_db
def test_read_only_user_cannot_trigger_refresh(api_client, read_user, workspace):
    api_client.force_authenticate(user=read_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_non_member_cannot_trigger_refresh(api_client, other_user, workspace):
    api_client.force_authenticate(user=other_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_refresh_status_returns_schema_state(manage_client, workspace, tenant):
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name="status_test_schema",
        state=SchemaState.ACTIVE,
    )
    resp = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")
    assert resp.status_code == 200
    assert resp.data["state"] == SchemaState.ACTIVE


@pytest.mark.django_db
def test_refresh_status_no_schema_returns_unavailable(manage_client, workspace):
    resp = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")
    assert resp.status_code == 200
    assert resp.data["state"] == "unavailable"
