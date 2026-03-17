"""Tests for workspace-scoped URL resolution.

These tests verify the URL structure where workspace_id is explicit in the URL path.
All content APIs are nested under /api/workspaces/<workspace_id>/.
"""

import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.fixture
def client():
    from django.test import Client

    return Client(enforce_csrf_checks=False)


@pytest.fixture
def user_a(db):
    return User.objects.create_user(email="user_a@test.com", password="pass")


@pytest.fixture
def user_b(db):
    return User.objects.create_user(email="user_b@test.com", password="pass")


@pytest.fixture
def tenant_a(db):
    from apps.users.models import Tenant

    return Tenant.objects.create(
        provider="commcare", external_id="domain-a", canonical_name="Domain A"
    )


@pytest.fixture
def tenant_b(db):
    from apps.users.models import Tenant

    return Tenant.objects.create(
        provider="commcare", external_id="domain-b", canonical_name="Domain B"
    )


@pytest.fixture
def membership_a(db, user_a, tenant_a):
    """Creates TenantMembership and auto-creates Workspace via signal."""
    from apps.users.models import TenantMembership

    return TenantMembership.objects.create(user=user_a, tenant=tenant_a)


@pytest.fixture
def membership_b(db, user_a, tenant_b):
    """Creates TenantMembership and auto-creates Workspace via signal."""
    from apps.users.models import TenantMembership

    return TenantMembership.objects.create(user=user_a, tenant=tenant_b)


@pytest.fixture
def workspace_a(membership_a):
    """The auto-created workspace for user_a in tenant_a."""
    from apps.workspaces.models import Workspace

    return Workspace.objects.get(
        is_auto_created=True,
        workspace_tenants__tenant=membership_a.tenant,
        memberships__user=membership_a.user,
    )


@pytest.fixture
def workspace_b(membership_b):
    """The auto-created workspace for user_a in tenant_b."""
    from apps.workspaces.models import Workspace

    return Workspace.objects.get(
        is_auto_created=True,
        workspace_tenants__tenant=membership_b.tenant,
        memberships__user=membership_b.user,
    )


class TestKnowledgeWorkspaceScoped:
    def test_scoped_url_returns_200(self, client, user_a, workspace_a):
        """GET /api/workspaces/<id>/knowledge/ returns 200 for a member."""
        client.force_login(user_a)
        response = client.get(f"/api/workspaces/{workspace_a.id}/knowledge/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a):
        """GET /api/knowledge/ (old URL) is no longer valid."""
        client.force_login(user_a)
        response = client.get("/api/knowledge/")
        assert response.status_code == 404

    def test_correct_workspace_is_used(self, client, user_a, workspace_a, workspace_b):
        """The workspace_id in the URL determines which workspace's data is returned."""
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(workspace=workspace_a, title="Entry A", content="content")
        KnowledgeEntry.objects.create(workspace=workspace_b, title="Entry B", content="content")

        client.force_login(user_a)
        response = client.get(f"/api/workspaces/{workspace_a.id}/knowledge/")
        assert response.status_code == 200
        titles = [item["title"] for item in response.json().get("results", [])]
        assert "Entry A" in titles
        assert "Entry B" not in titles

    def test_non_member_returns_403(self, client, user_b, workspace_a):
        """A workspace_id for a workspace the user doesn't belong to returns 403."""
        client.force_login(user_b)
        response = client.get(f"/api/workspaces/{workspace_a.id}/knowledge/")
        assert response.status_code == 403


class TestRecipesWorkspaceScoped:
    def test_scoped_url_returns_200(self, client, user_a, workspace_a):
        client.force_login(user_a)
        response = client.get(f"/api/workspaces/{workspace_a.id}/recipes/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a):
        client.force_login(user_a)
        response = client.get("/api/recipes/")
        assert response.status_code == 404

    def test_correct_workspace_is_used(self, client, user_a, workspace_a, workspace_b):
        from apps.recipes.models import Recipe

        Recipe.objects.create(workspace=workspace_a, name="Recipe A")
        Recipe.objects.create(workspace=workspace_b, name="Recipe B")

        client.force_login(user_a)
        response = client.get(f"/api/workspaces/{workspace_a.id}/recipes/")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert "Recipe A" in names
        assert "Recipe B" not in names

    def test_non_member_returns_403(self, client, user_b, workspace_a):
        client.force_login(user_b)
        response = client.get(f"/api/workspaces/{workspace_a.id}/recipes/")
        assert response.status_code == 403


class TestArtifactsWorkspaceScoped:
    def test_scoped_url_returns_200(self, client, user_a, workspace_a):
        client.force_login(user_a)
        response = client.get(f"/api/workspaces/{workspace_a.id}/artifacts/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a):
        client.force_login(user_a)
        response = client.get("/api/artifacts/")
        assert response.status_code == 404

    def test_non_member_returns_403(self, client, user_b, workspace_a):
        client.force_login(user_b)
        response = client.get(f"/api/workspaces/{workspace_a.id}/artifacts/")
        assert response.status_code == 403


class TestDataDictionaryWorkspaceScoped:
    def test_scoped_url_returns_503_when_no_schema(self, client, user_a, workspace_a):
        """Without an active schema, data-dictionary returns 503 (not 200)."""
        client.force_login(user_a)
        response = client.get(f"/api/workspaces/{workspace_a.id}/data-dictionary/")
        assert response.status_code == 503

    def test_unscoped_url_returns_404(self, client, user_a):
        client.force_login(user_a)
        response = client.get("/api/data-dictionary/")
        assert response.status_code == 404

    def test_non_member_returns_403(self, client, user_b, workspace_a):
        client.force_login(user_b)
        response = client.get(f"/api/workspaces/{workspace_a.id}/data-dictionary/")
        assert response.status_code == 403

    def test_each_workspace_returns_503_without_schema(
        self, client, user_a, workspace_a, workspace_b
    ):
        """Without active schemas, each workspace endpoint returns 503."""
        client.force_login(user_a)
        response_a = client.get(f"/api/workspaces/{workspace_a.id}/data-dictionary/")
        response_b = client.get(f"/api/workspaces/{workspace_b.id}/data-dictionary/")
        assert response_a.status_code == 503
        assert response_b.status_code == 503


class TestRefreshSchemaWorkspaceScoped:
    def test_scoped_url_returns_202(self, client, user_a, workspace_a, membership_a):
        """Refresh endpoint returns 202 Accepted and queues a background task."""
        from unittest.mock import patch

        client.force_login(user_a)
        with patch("apps.workspaces.api.views.transaction.on_commit"):
            response = client.post(f"/api/workspaces/{workspace_a.id}/refresh/")
        assert response.status_code == 202

    def test_non_member_returns_403(self, client, user_b, workspace_a):
        client.force_login(user_b)
        response = client.post(f"/api/workspaces/{workspace_a.id}/refresh/")
        assert response.status_code == 403


class TestTableDetailWorkspaceScoped:
    def test_scoped_url_returns_503_without_schema(self, client, user_a, workspace_a):
        """Without an active schema, table detail returns 503."""
        client.force_login(user_a)
        response = client.get(
            f"/api/workspaces/{workspace_a.id}/data-dictionary/tables/public.nonexistent/"
        )
        assert response.status_code == 503

    def test_non_member_returns_403(self, client, user_b, workspace_a):
        client.force_login(user_b)
        response = client.get(
            f"/api/workspaces/{workspace_a.id}/data-dictionary/tables/public.cases/"
        )
        assert response.status_code == 403
