from unittest.mock import AsyncMock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.test import Client

from apps.users.models import Tenant, TenantMembership


@pytest.mark.django_db
class TestTenantEnsureAPI:
    def _create_connect_social_token(self, user):
        """Create the allauth SocialApp, SocialAccount, and SocialToken for commcare_connect."""

        app = SocialApp.objects.create(
            provider="commcare_connect",
            name="Connect",
            client_id="test",
            secret="test",
        )
        account = SocialAccount.objects.create(
            user=user,
            provider="commcare_connect",
            uid="123",
        )
        SocialToken.objects.create(app=app, account=account, token="fake-token")

    def test_ensure_creates_connect_membership(self, user):
        self._create_connect_social_token(user)

        tenant = Tenant.objects.create(
            provider="commcare_connect", external_id="532", canonical_name="Opportunity 532"
        )
        tm_obj = TenantMembership.objects.create(user=user, tenant=tenant)

        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.resolve_connect_opportunities",
            new=AsyncMock(return_value=[tm_obj]),
        ):
            response = client.post(
                "/api/auth/tenants/ensure/",
                data={"provider": "commcare_connect", "tenant_id": "532"},
                content_type="application/json",
            )
        assert response.status_code == 200

        data = response.json()
        assert data["provider"] == "commcare_connect"
        assert data["tenant_id"] == "532"
        assert data["tenant_name"] == "Opportunity 532"
        assert "id" in data

        tm = TenantMembership.objects.get(
            user=user, tenant__provider="commcare_connect", tenant__external_id="532"
        )
        assert tm.last_selected_at is not None

    def test_ensure_returns_404_for_unauthorized_opportunity(self, user):
        self._create_connect_social_token(user)

        # Return memberships that don't include tenant_id 999
        other_tenant = Tenant.objects.create(
            provider="commcare_connect", external_id="532", canonical_name="Opportunity 532"
        )
        other_tm = TenantMembership.objects.create(user=user, tenant=other_tenant)

        client = Client()
        client.force_login(user)

        with patch(
            "apps.users.views.resolve_connect_opportunities",
            new=AsyncMock(return_value=[other_tm]),
        ):
            response = client.post(
                "/api/auth/tenants/ensure/",
                data={"provider": "commcare_connect", "tenant_id": "999"},
                content_type="application/json",
            )
        assert response.status_code == 404

    def test_ensure_returns_existing_membership(self, user):
        self._create_connect_social_token(user)

        existing_tenant = Tenant.objects.create(
            provider="commcare_connect", external_id="532", canonical_name="Existing Opp"
        )
        TenantMembership.objects.create(user=user, tenant=existing_tenant)

        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/auth/tenants/ensure/",
            data={"provider": "commcare_connect", "tenant_id": "532"},
            content_type="application/json",
        )
        assert response.status_code == 200

        data = response.json()
        assert data["tenant_id"] == "532"
        assert data["tenant_name"] == "Existing Opp"

        assert (
            TenantMembership.objects.filter(
                user=user, tenant__provider="commcare_connect", tenant__external_id="532"
            ).count()
            == 1
        )

    def test_ensure_returns_404_without_oauth_token(self, user):
        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/auth/tenants/ensure/",
            data={"provider": "commcare_connect", "tenant_id": "532"},
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_ensure_requires_auth(self):
        client = Client()
        response = client.post(
            "/api/auth/tenants/ensure/",
            data={"provider": "commcare_connect", "tenant_id": "532"},
            content_type="application/json",
        )
        assert response.status_code == 401
