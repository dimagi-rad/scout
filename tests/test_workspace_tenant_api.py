import pytest
from rest_framework.test import APIClient

from apps.projects.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.users.models import Tenant, TenantMembership


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="api@example.com", password="pass")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="api-domain", canonical_name="API Domain"
    )


@pytest.fixture
def tenant2(db):
    return Tenant.objects.create(
        provider="commcare", external_id="api-domain-2", canonical_name="API Domain 2"
    )


@pytest.fixture
def workspace(db, user, tenant):
    ws = Workspace.objects.create(name="API WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    return ws


@pytest.fixture
def tenant_membership(db, user, tenant2):
    return TenantMembership.objects.create(user=user, tenant=tenant2)


def test_add_tenant_to_workspace(api_client, user, workspace, tenant2, tenant_membership):
    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 202, resp.data
    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant2).exists()


def test_add_tenant_requires_manage_role(api_client, user, workspace, tenant2):
    from django.contrib.auth import get_user_model

    other = get_user_model().objects.create_user(email="other@example.com", password="pass")
    WorkspaceMembership.objects.create(
        workspace=workspace, user=other, role=WorkspaceRole.READ_WRITE
    )
    api_client.force_login(other)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 403


def test_add_tenant_user_lacks_tenant_membership_is_rejected(api_client, user, workspace, tenant2):
    # user has no TenantMembership for tenant2
    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 400
    assert "access" in resp.data["error"].lower()


def test_add_tenant_already_in_workspace_is_idempotent(api_client, user, workspace, tenant):
    # tenant is already in workspace
    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant.id)},
        format="json",
    )
    assert resp.status_code == 200  # idempotent OK


def test_remove_tenant_from_workspace(api_client, user, workspace, tenant2, tenant_membership):
    wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    api_client.force_login(user)
    resp = api_client.delete(f"/api/workspaces/{workspace.id}/tenants/{wt.id}/")
    assert resp.status_code == 204
    assert not WorkspaceTenant.objects.filter(id=wt.id).exists()


def test_cannot_remove_last_tenant_from_workspace(api_client, user, workspace, tenant):
    wt = WorkspaceTenant.objects.get(workspace=workspace, tenant=tenant)
    api_client.force_login(user)
    resp = api_client.delete(f"/api/workspaces/{workspace.id}/tenants/{wt.id}/")
    assert resp.status_code == 400
    assert "last" in resp.data["error"].lower()
