from unittest.mock import patch

import pytest
from django.db.models import ProtectedError
from rest_framework.test import APIClient

from apps.chat.models import Thread
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.api import workspace_views
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.workspace_service import LastWorkspaceTenant
from tests.tenant_access import grant_tenant_access


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def tenant2(db):
    return Tenant.objects.create(
        provider="commcare", external_id="api-domain-2", canonical_name="API Domain 2"
    )


@pytest.fixture
def tenant_membership(db, user, tenant2):
    """Grant the test user access to tenant2 (used for add/remove tenant tests)."""
    return grant_tenant_access(user, tenant2)


def test_add_tenant_to_workspace(api_client, user, workspace, tenant2, tenant_membership):
    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 202, resp.data
    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant2).exists()


def test_add_tenant_requires_manage_role(api_client, user, workspace, tenant, tenant2):
    from django.contrib.auth import get_user_model

    other = get_user_model().objects.create_user(email="other@example.com", password="pass")
    WorkspaceMembership.objects.create(
        workspace=workspace, user=other, role=WorkspaceRole.READ_WRITE
    )
    grant_tenant_access(other, tenant)
    api_client.force_login(other)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )
    assert resp.status_code == 403
    assert resp.data["error"] == "Only workspace managers can add tenants."


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
    # user already holds a live TenantMembership for `tenant` (via the workspace fixture)
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


@pytest.fixture
def sibling_workspace(db, user, tenant):
    """Another workspace of ``user``'s over ``tenant``, so deleting ``workspace`` is
    not refused as their last one covering it."""
    ws = Workspace.objects.create(name="Sibling", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    return ws


def _last_source_url(workspace, tenant, *, confirm=False):
    wt = WorkspaceTenant.objects.get(workspace=workspace, tenant=tenant)
    suffix = "?confirm_delete_workspace=true" if confirm else ""
    return f"/api/workspaces/{workspace.id}/tenants/{wt.id}/{suffix}"


def test_removing_last_source_asks_to_confirm_deleting_the_workspace(
    api_client, user, workspace, tenant, sibling_workspace
):
    Thread.objects.create(workspace=workspace, user=user)
    api_client.force_login(user)

    resp = api_client.delete(_last_source_url(workspace, tenant))

    assert resp.status_code == 409, resp.data
    assert resp.data["requires_confirmation"] == "delete_workspace"
    assert resp.data["member_count"] == 1
    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).exists()
    assert Thread.objects.filter(workspace=workspace).exists()


def test_removing_last_source_with_confirmation_deletes_the_workspace(
    api_client, user, workspace, tenant, sibling_workspace
):
    Thread.objects.create(workspace=workspace, user=user)
    api_client.force_login(user)

    resp = api_client.delete(_last_source_url(workspace, tenant, confirm=True))

    assert resp.status_code == 200, resp.data
    assert resp.data == {"workspace_deleted": True}
    assert not Workspace.objects.filter(id=workspace.id).exists()
    assert not Thread.objects.filter(workspace_id=workspace.id).exists()
    assert WorkspaceTenant.objects.filter(workspace=sibling_workspace, tenant=tenant).exists()


def test_source_added_before_the_confirmed_delete_keeps_the_workspace(
    api_client, user, workspace, tenant, tenant2, sibling_workspace
):
    """A source that lands between the last-source check and the delete wins."""
    url = _last_source_url(workspace, tenant, confirm=True)
    WorkspaceTenant.objects.create(workspace=sibling_workspace, tenant=tenant2)
    real_remove = workspace_views.remove_workspace_tenant
    calls = []

    def last_then_real(ws, wt):
        calls.append(wt.id)
        if len(calls) == 1:
            WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
            raise LastWorkspaceTenant("Cannot remove the last tenant from a workspace.")
        return real_remove(ws, wt)

    api_client.force_login(user)
    with patch.object(workspace_views, "remove_workspace_tenant", side_effect=last_then_real):
        resp = api_client.delete(url)

    assert resp.status_code == 204
    assert Workspace.objects.filter(id=workspace.id).exists()
    assert list(workspace.workspace_tenants.values_list("tenant_id", flat=True)) == [tenant2.id]


def test_a_double_submitted_confirmed_delete_is_idempotent(
    api_client, user, workspace, tenant, sibling_workspace
):
    url = _last_source_url(workspace, tenant, confirm=True)

    def other_request_deleted_it(ws, wt):
        Workspace.objects.filter(id=ws.id).delete()
        raise LastWorkspaceTenant("Cannot remove the last tenant from a workspace.")

    api_client.force_login(user)
    with patch.object(
        workspace_views, "remove_workspace_tenant", side_effect=other_request_deleted_it
    ):
        resp = api_client.delete(url)

    assert resp.status_code == 200
    assert resp.data == {"workspace_deleted": True}


def test_non_manager_cannot_remove_last_source_even_confirmed(
    api_client, workspace, tenant, write_user
):
    api_client.force_login(write_user)

    resp = api_client.delete(_last_source_url(workspace, tenant, confirm=True))

    assert resp.status_code == 403
    assert resp.data["error"] == "Only workspace managers can remove tenants."
    assert Workspace.objects.filter(id=workspace.id).exists()
    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).exists()


def test_removing_last_source_keeps_the_last_workspace_covering_it(
    api_client, user, workspace, tenant
):
    """Workspace delete's guard applies, and is checked before asking to confirm."""
    api_client.force_login(user)

    resp = api_client.delete(_last_source_url(workspace, tenant, confirm=True))

    assert resp.status_code == 400
    assert resp.data["error"].startswith("Removing the last data source deletes the workspace.")
    assert "last workspace covering a tenant" in resp.data["error"]
    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).exists()


def test_removing_a_missing_last_source_of_a_shared_workspace_is_refused(
    settings, api_client, user, workspace, tenant, read_user
):
    """Workspace delete's refusal for a manager missing a source of a shared workspace."""
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = True
    TenantMembership.objects.filter(user=user, tenant=tenant).delete()
    api_client.force_login(user)

    resp = api_client.delete(_last_source_url(workspace, tenant, confirm=True))

    assert resp.status_code == 403
    assert "shared workspace" in resp.data["error"]
    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).exists()


def test_removing_a_missing_last_source_of_an_unshared_workspace_deletes_it(
    settings, api_client, user, workspace, tenant
):
    """The source they lack doesn't count as covered, so this is not their last one."""
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = True
    TenantMembership.objects.filter(user=user, tenant=tenant).delete()
    api_client.force_login(user)

    resp = api_client.delete(_last_source_url(workspace, tenant, confirm=True))

    assert resp.status_code == 200, resp.data
    assert not Workspace.objects.filter(id=workspace.id).exists()


@pytest.mark.parametrize("via_queryset", [False, True])
def test_deleting_a_tenant_a_workspace_uses_is_blocked(workspace, tenant, via_queryset):
    """A tenant delete would otherwise cascade to the link and empty the workspace."""
    with pytest.raises(ProtectedError):
        if via_queryset:
            Tenant.objects.filter(id=tenant.id).delete()
        else:
            tenant.delete()

    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).exists()


def test_deleting_a_tenant_no_workspace_uses_still_works(tenant2):
    tenant2.delete()

    assert not Tenant.objects.filter(id=tenant2.id).exists()


def test_add_tenant_refused_when_member_lacks_a_workspace_tenant(
    api_client, user, workspace, tenant, tenant2
):
    """A member lacking one of the workspace's tenants cannot manage its sources at all."""
    # tenant2 is already in workspace via ORM, but user has NO TenantMembership for tenant2
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)

    api_client.force_login(user)
    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )

    # Under all-of access a member lacking tenant2 cannot reach the workspace at
    # all, so the re-add is refused before it gets to the tenant check.
    assert resp.status_code == 403
    assert resp.data["reason"] == "tenant_access_lost"


def test_add_tenant_already_in_workspace_still_checks_the_requester_under_any_of(
    settings, api_client, user, workspace, tenant, tenant2
):
    """With the rollout switch off the gate lets them in; the re-add still checks."""
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = False
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    api_client.force_login(user)

    resp = api_client.post(
        f"/api/workspaces/{workspace.id}/tenants/",
        {"tenant_id": str(tenant2.id)},
        format="json",
    )

    assert resp.status_code == 400
    assert "do not have access" in resp.data["error"]


def test_list_workspace_tenants(api_client, user, workspace):
    """GET /api/workspaces/<id>/tenants/ returns current tenants."""
    api_client.force_login(user)
    response = api_client.get(f"/api/workspaces/{workspace.id}/tenants/")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    # workspace fixture has one tenant (defined in this file's fixtures)
    assert len(data) == 1
    assert "id" in data[0]  # WorkspaceTenant UUID
    assert "tenant_id" in data[0]  # internal Tenant UUID
    assert "tenant_name" in data[0]
    assert "provider" in data[0]
