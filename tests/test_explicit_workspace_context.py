"""Tests for path-scoped tenant workspace resolution.

These tests verify the NEW URL structure where tenant_id is explicit in the URL path.
They FAIL against the current code (old unscoped URLs) and PASS after the fix.
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
    from apps.users.models import TenantMembership

    return TenantMembership.objects.create(user=user_a, tenant=tenant_a)


@pytest.fixture
def membership_b(db, user_a, tenant_b):
    from apps.users.models import TenantMembership

    return TenantMembership.objects.create(user=user_a, tenant=tenant_b)


@pytest.fixture
def workspace_a(db, tenant_a):
    from apps.projects.models import TenantWorkspace

    ws, _ = TenantWorkspace.objects.get_or_create(tenant=tenant_a)
    return ws


@pytest.fixture
def workspace_b(db, tenant_b):
    from apps.projects.models import TenantWorkspace

    ws, _ = TenantWorkspace.objects.get_or_create(tenant=tenant_b)
    return ws


class TestKnowledgeExplicitTenantId:
    def test_scoped_url_returns_200(self, client, user_a, membership_a, workspace_a):
        """GET /api/knowledge/<tenant_id>/ returns 200 for a valid tenant."""
        client.force_login(user_a)
        response = client.get(f"/api/knowledge/{membership_a.id}/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a, membership_a, workspace_a):
        """GET /api/knowledge/ (old URL) is no longer valid."""
        client.force_login(user_a)
        response = client.get("/api/knowledge/")
        assert response.status_code == 404

    def test_correct_workspace_is_used(
        self, client, user_a, membership_a, membership_b, workspace_a, workspace_b
    ):
        """The tenant_id in the URL determines which workspace's data is returned."""
        from apps.knowledge.models import KnowledgeEntry

        KnowledgeEntry.objects.create(workspace=workspace_a, title="Entry A", content="content")
        KnowledgeEntry.objects.create(workspace=workspace_b, title="Entry B", content="content")

        # Mark membership_b as most recently selected — must have no effect
        from django.utils import timezone

        membership_b.last_selected_at = timezone.now()
        membership_b.save(update_fields=["last_selected_at"])

        client.force_login(user_a)

        response = client.get(f"/api/knowledge/{membership_a.id}/")
        assert response.status_code == 200
        items = response.json().get("results", [])
        titles = [item["title"] for item in items]
        assert "Entry A" in titles
        assert "Entry B" not in titles

    def test_foreign_tenant_id_returns_403(self, client, user_a, user_b, db):
        """A tenant_id belonging to another user returns 403."""
        from apps.users.models import Tenant, TenantMembership

        other_tenant = Tenant.objects.create(
            provider="commcare", external_id="domain-other", canonical_name="Other"
        )
        other_membership = TenantMembership.objects.create(user=user_b, tenant=other_tenant)
        client.force_login(user_a)
        response = client.get(f"/api/knowledge/{other_membership.id}/")
        assert response.status_code == 403


class TestRecipesExplicitTenantId:
    def test_scoped_url_returns_200(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get(f"/api/recipes/{membership_a.id}/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get("/api/recipes/")
        assert response.status_code == 404

    def test_correct_workspace_is_used(
        self, client, user_a, membership_a, membership_b, workspace_a, workspace_b
    ):
        from apps.recipes.models import Recipe

        Recipe.objects.create(workspace=workspace_a, name="Recipe A")
        Recipe.objects.create(workspace=workspace_b, name="Recipe B")

        from django.utils import timezone

        membership_b.last_selected_at = timezone.now()
        membership_b.save(update_fields=["last_selected_at"])

        client.force_login(user_a)
        response = client.get(f"/api/recipes/{membership_a.id}/")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert "Recipe A" in names
        assert "Recipe B" not in names

    def test_foreign_tenant_id_returns_403(self, client, user_a, user_b, db):
        from apps.users.models import Tenant, TenantMembership

        other_tenant = Tenant.objects.create(
            provider="commcare", external_id="domain-other2", canonical_name="Other2"
        )
        other_membership = TenantMembership.objects.create(user=user_b, tenant=other_tenant)
        client.force_login(user_a)
        response = client.get(f"/api/recipes/{other_membership.id}/")
        assert response.status_code == 403


class TestArtifactsExplicitTenantId:
    def test_scoped_url_returns_200(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get(f"/api/artifacts/{membership_a.id}/")
        assert response.status_code == 200

    def test_unscoped_url_returns_404(self, client, user_a, membership_a, workspace_a):
        client.force_login(user_a)
        response = client.get("/api/artifacts/")
        assert response.status_code == 404

    def test_foreign_tenant_id_returns_403(self, client, user_a, user_b, db):
        from apps.users.models import Tenant, TenantMembership

        other_tenant = Tenant.objects.create(
            provider="commcare", external_id="domain-other3", canonical_name="Other3"
        )
        other_membership = TenantMembership.objects.create(user=user_b, tenant=other_tenant)
        client.force_login(user_a)
        response = client.get(f"/api/artifacts/{other_membership.id}/")
        assert response.status_code == 403
