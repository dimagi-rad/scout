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
class TestKnowledgeProvenance:
    def test_knowledge_list_in_custom_workspace_shows_source(
        self, api_client, owner, custom_workspace, tenant_workspace_a
    ):
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(
            workspace=tenant_workspace_a,
            title="Tenant Entry",
            content="From tenant",
        )
        KnowledgeEntry.objects.create(
            custom_workspace=custom_workspace,
            title="Workspace Entry",
            content="From workspace",
        )

        api_client.force_login(owner)
        response = api_client.get(
            "/api/knowledge/",
            HTTP_X_CUSTOM_WORKSPACE=str(custom_workspace.id),
        )
        assert response.status_code == 200
        entries = response.json()["results"]
        sources = {e["title"]: e.get("source") for e in entries}
        assert sources["Tenant Entry"] == "tenant"
        assert sources["Workspace Entry"] == "workspace"

    def test_knowledge_list_in_custom_workspace_shows_source_name(
        self, api_client, owner, custom_workspace, tenant_workspace_a
    ):
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(
            workspace=tenant_workspace_a,
            title="Tenant Entry",
            content="From tenant",
        )
        KnowledgeEntry.objects.create(
            custom_workspace=custom_workspace,
            title="Workspace Entry",
            content="From workspace",
        )

        api_client.force_login(owner)
        response = api_client.get(
            "/api/knowledge/",
            HTTP_X_CUSTOM_WORKSPACE=str(custom_workspace.id),
        )
        assert response.status_code == 200
        entries = response.json()["results"]
        source_names = {e["title"]: e.get("source_name") for e in entries}
        assert source_names["Tenant Entry"] == "Domain A"
        assert source_names["Workspace Entry"] == "This Workspace"

    def test_knowledge_list_without_header_no_source_fields(
        self, api_client, owner, custom_workspace, tenant_workspace_a, owner_memberships
    ):
        """Without X-Custom-Workspace header, source fields should not appear."""
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(
            workspace=tenant_workspace_a,
            title="Regular Entry",
            content="Normal mode",
        )

        api_client.force_login(owner)
        response = api_client.get("/api/knowledge/")
        assert response.status_code == 200
        entries = response.json()["results"]
        if entries:
            assert "source" not in entries[0]
            assert "source_name" not in entries[0]

    def test_knowledge_list_custom_workspace_nonmember_denied(
        self, api_client, other_user, custom_workspace
    ):
        """Non-members of the custom workspace should be denied."""
        api_client.force_login(other_user)
        response = api_client.get(
            "/api/knowledge/",
            HTTP_X_CUSTOM_WORKSPACE=str(custom_workspace.id),
        )
        assert response.status_code == 403
