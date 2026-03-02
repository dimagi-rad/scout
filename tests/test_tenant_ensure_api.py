import pytest
from django.test import Client

from apps.users.models import TenantMembership


@pytest.mark.django_db
class TestTenantEnsureAPI:
    def _create_connect_social_token(self, user):
        """Create the allauth SocialApp, SocialAccount, and SocialToken for commcare_connect."""
        from allauth.socialaccount.models import SocialApp, SocialAccount, SocialToken

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

        client = Client()
        client.force_login(user)

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

        tm = TenantMembership.objects.get(user=user, provider="commcare_connect", tenant_id="532")
        assert tm.last_selected_at is not None

    def test_ensure_returns_existing_membership(self, user):
        self._create_connect_social_token(user)

        TenantMembership.objects.create(
            user=user,
            provider="commcare_connect",
            tenant_id="532",
            tenant_name="Existing Opp",
        )

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

        assert TenantMembership.objects.filter(
            user=user, provider="commcare_connect", tenant_id="532"
        ).count() == 1

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
