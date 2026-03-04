"""Tests for CustomWorkspace REST API endpoints."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.users.models import TenantMembership
from apps.workspace.models import (
    CustomWorkspace,
    CustomWorkspaceTenant,
    TenantWorkspace,
    WorkspaceMembership,
)

User = get_user_model()


@pytest.fixture
def api_client():
    return Client()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@test.com", password="testpass123")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="other@test.com", password="testpass123")


@pytest.fixture
def tenant_workspace_a(db):
    return TenantWorkspace.objects.create(tenant_id="domain-a", tenant_name="Domain A")


@pytest.fixture
def tenant_workspace_b(db):
    return TenantWorkspace.objects.create(tenant_id="domain-b", tenant_name="Domain B")


@pytest.fixture
def owner_memberships(owner, tenant_workspace_a, tenant_workspace_b):
    TenantMembership.objects.create(
        user=owner, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
    )
    TenantMembership.objects.create(
        user=owner, provider="commcare", tenant_id="domain-b", tenant_name="Domain B"
    )


@pytest.fixture
def other_user_partial_access(other_user):
    """other_user only has access to domain-a, not domain-b."""
    TenantMembership.objects.create(
        user=other_user, provider="commcare", tenant_id="domain-a", tenant_name="Domain A"
    )


@pytest.fixture
def custom_workspace(owner, tenant_workspace_a, tenant_workspace_b, owner_memberships):
    ws = CustomWorkspace.objects.create(name="Test Workspace", created_by=owner)
    CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_a)
    CustomWorkspaceTenant.objects.create(workspace=ws, tenant_workspace=tenant_workspace_b)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


@pytest.mark.django_db
class TestCustomWorkspaceList:
    def test_list_returns_only_user_workspaces(self, api_client, owner, custom_workspace):
        api_client.force_login(owner)
        response = api_client.get("/api/custom-workspaces/")
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["name"] == "Test Workspace"

    def test_list_excludes_non_member_workspaces(self, api_client, other_user, custom_workspace):
        api_client.force_login(other_user)
        response = api_client.get("/api/custom-workspaces/")
        assert response.status_code == 200
        assert len(response.json()) == 0

    def test_unauthenticated_returns_403(self, api_client):
        response = api_client.get("/api/custom-workspaces/")
        assert response.status_code == 403


@pytest.mark.django_db
class TestCustomWorkspaceCreate:
    def test_create_workspace(self, api_client, owner, tenant_workspace_a, owner_memberships):
        api_client.force_login(owner)
        response = api_client.post(
            "/api/custom-workspaces/",
            data={"name": "New Workspace", "tenant_workspace_ids": [str(tenant_workspace_a.id)]},
            content_type="application/json",
        )
        assert response.status_code == 201
        ws = CustomWorkspace.objects.get(name="New Workspace")
        assert ws.created_by == owner
        assert ws.custom_workspace_tenants.count() == 1
        assert WorkspaceMembership.objects.filter(workspace=ws, user=owner, role="owner").exists()


@pytest.mark.django_db
class TestCustomWorkspaceEnter:
    def test_enter_workspace_success(self, api_client, owner, custom_workspace):
        api_client.force_login(owner)
        response = api_client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 200

    def test_enter_blocked_when_missing_tenant_access(
        self, api_client, other_user, custom_workspace, other_user_partial_access
    ):
        WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="viewer"
        )
        api_client.force_login(other_user)
        response = api_client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 403
        data = response.json()
        assert "domain-b" in str(data.get("missing_tenants", []))

    def test_enter_blocked_for_non_member(self, api_client, other_user, custom_workspace):
        api_client.force_login(other_user)
        response = api_client.post(f"/api/custom-workspaces/{custom_workspace.id}/enter/")
        assert response.status_code == 403


@pytest.mark.django_db
class TestEnsureWorkspaceForTenant:
    url = "/api/custom-workspaces/ensure-for-tenant/"

    def test_creates_workspace_when_none_exists(
        self, api_client, owner, tenant_workspace_a, owner_memberships
    ):
        api_client.force_login(owner)
        response = api_client.post(
            self.url,
            data={"tenant_id": "domain-a"},
            content_type="application/json",
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Domain A"
        assert len(data["tenants"]) == 1
        assert data["tenants"][0]["tenant_id"] == "domain-a"

    def test_returns_existing_single_tenant_workspace(
        self, api_client, owner, tenant_workspace_a, owner_memberships
    ):
        """Idempotent: second call returns the same workspace."""
        api_client.force_login(owner)
        r1 = api_client.post(
            self.url,
            data={"tenant_id": "domain-a"},
            content_type="application/json",
        )
        r2 = api_client.post(
            self.url,
            data={"tenant_id": "domain-a"},
            content_type="application/json",
        )
        assert r1.json()["id"] == r2.json()["id"]
        assert r2.status_code == 200

    def test_ignores_multi_tenant_workspaces(
        self, api_client, owner, custom_workspace, tenant_workspace_a, owner_memberships
    ):
        """custom_workspace has 2 tenants — ensure-for-tenant should create a new one."""
        api_client.force_login(owner)
        response = api_client.post(
            self.url,
            data={"tenant_id": "domain-a"},
            content_type="application/json",
        )
        assert response.status_code == 201
        assert response.json()["id"] != str(custom_workspace.id)

    def test_404_when_no_tenant_membership(self, api_client, other_user, tenant_workspace_a):
        api_client.force_login(other_user)
        response = api_client.post(
            self.url,
            data={"tenant_id": "domain-a"},
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_400_when_tenant_id_missing(self, api_client, owner, owner_memberships):
        api_client.force_login(owner)
        response = api_client.post(
            self.url,
            data={},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_repeated_calls_return_same_workspace(
        self, api_client, owner, tenant_workspace_a, owner_memberships
    ):
        """Three sequential calls should all return the same workspace id."""
        api_client.force_login(owner)
        ids = []
        for _ in range(3):
            r = api_client.post(
                self.url,
                data={"tenant_id": "domain-a"},
                content_type="application/json",
            )
            ids.append(r.json()["id"])
        assert len(set(ids)) == 1


@pytest.mark.django_db
class TestOwnerRoleProtection:
    """Test that owner-only actions are properly restricted."""

    def test_viewer_cannot_delete_workspace(
        self, api_client, other_user, custom_workspace, other_user_partial_access
    ):
        # Give other_user full tenant access + viewer role
        TenantMembership.objects.get_or_create(
            user=other_user,
            provider="commcare",
            tenant_id="domain-b",
            defaults={"tenant_name": "Domain B"},
        )
        WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="viewer"
        )
        api_client.force_login(other_user)
        response = api_client.delete(f"/api/custom-workspaces/{custom_workspace.id}/")
        assert response.status_code == 403

    def test_editor_cannot_delete_workspace(
        self, api_client, other_user, custom_workspace, other_user_partial_access
    ):
        TenantMembership.objects.get_or_create(
            user=other_user,
            provider="commcare",
            tenant_id="domain-b",
            defaults={"tenant_name": "Domain B"},
        )
        WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="editor"
        )
        api_client.force_login(other_user)
        response = api_client.delete(f"/api/custom-workspaces/{custom_workspace.id}/")
        assert response.status_code == 403

    def test_viewer_cannot_add_member(
        self, api_client, other_user, custom_workspace, other_user_partial_access
    ):
        TenantMembership.objects.get_or_create(
            user=other_user,
            provider="commcare",
            tenant_id="domain-b",
            defaults={"tenant_name": "Domain B"},
        )
        WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="viewer"
        )
        api_client.force_login(other_user)
        response = api_client.post(
            f"/api/custom-workspaces/{custom_workspace.id}/members/",
            data={"user_id": str(other_user.id), "role": "viewer"},
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_editor_can_update_workspace(
        self, api_client, other_user, custom_workspace, other_user_partial_access
    ):
        TenantMembership.objects.get_or_create(
            user=other_user,
            provider="commcare",
            tenant_id="domain-b",
            defaults={"tenant_name": "Domain B"},
        )
        WorkspaceMembership.objects.create(
            workspace=custom_workspace, user=other_user, role="editor"
        )
        api_client.force_login(other_user)
        response = api_client.patch(
            f"/api/custom-workspaces/{custom_workspace.id}/",
            data={"name": "Updated Name"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated Name"


@pytest.mark.django_db
class TestCanPerformAction:
    """Test the ROLE_PERMISSIONS map."""

    def test_owner_can_do_everything(self):
        from apps.workspace.api.views import _can_perform_action

        for action in ("read", "write", "manage_members", "manage_tenants", "delete"):
            assert _can_perform_action("owner", action)

    def test_editor_can_read_and_write(self):
        from apps.workspace.api.views import _can_perform_action

        assert _can_perform_action("editor", "read")
        assert _can_perform_action("editor", "write")
        assert not _can_perform_action("editor", "manage_members")
        assert not _can_perform_action("editor", "delete")

    def test_viewer_can_only_read(self):
        from apps.workspace.api.views import _can_perform_action

        assert _can_perform_action("viewer", "read")
        assert not _can_perform_action("viewer", "write")
        assert not _can_perform_action("viewer", "manage_members")

    def test_unknown_role_has_no_permissions(self):
        from apps.workspace.api.views import _can_perform_action

        assert not _can_perform_action("unknown", "read")
