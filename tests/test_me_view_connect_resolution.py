"""Tests for me_view Connect opportunity resolution."""

from unittest.mock import patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site

from apps.users.models import Tenant, TenantCredential, TenantMembership

User = get_user_model()


@pytest.fixture
def site(db):
    site, _ = Site.objects.get_or_create(id=1, defaults={"domain": "testserver", "name": "Test"})
    return site


@pytest.fixture
def commcare_app(site):
    app = SocialApp.objects.create(
        provider="commcare", name="CommCare", client_id="cc-client", secret="cc-secret",
    )
    app.sites.add(site)
    return app


@pytest.fixture
def connect_app(site):
    app = SocialApp.objects.create(
        provider="commcare_connect", name="Connect", client_id="connect-client", secret="connect-secret",
    )
    app.sites.add(site)
    return app


@pytest.fixture
def user_with_both_tokens(db, commcare_app, connect_app):
    user = User.objects.create_user(email="both@example.com", password="pass")
    cc_account = SocialAccount.objects.create(user=user, provider="commcare", uid="cc-uid")
    SocialToken.objects.create(app=commcare_app, account=cc_account, token="cc-token")
    connect_account = SocialAccount.objects.create(user=user, provider="commcare_connect", uid="connect-uid")
    SocialToken.objects.create(app=connect_app, account=connect_account, token="connect-token")
    return user


@pytest.mark.django_db
class TestMeViewResolveBothProviders:
    def test_connect_resolved_even_when_commcare_succeeds(self, client, user_with_both_tokens):
        client.force_login(user_with_both_tokens)
        commcare_called = False
        connect_called = False

        def mock_resolve_commcare(user, token):
            nonlocal commcare_called
            commcare_called = True
            t, _ = Tenant.objects.get_or_create(provider="commcare", external_id="test-domain", defaults={"canonical_name": "Test Domain"})
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=t)
            TenantCredential.objects.get_or_create(tenant_membership=tm, defaults={"credential_type": TenantCredential.OAUTH})
            return [tm]

        def mock_resolve_connect(user, token):
            nonlocal connect_called
            connect_called = True
            t, _ = Tenant.objects.get_or_create(provider="commcare_connect", external_id="opp-1", defaults={"canonical_name": "Test Opp"})
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=t)
            TenantCredential.objects.get_or_create(tenant_membership=tm, defaults={"credential_type": TenantCredential.OAUTH})
            return [tm]

        with patch("apps.users.auth_views.resolve_commcare_domains", side_effect=mock_resolve_commcare), \
             patch("apps.users.auth_views.resolve_connect_opportunities", side_effect=mock_resolve_connect):
            resp = client.get("/api/auth/me/")

        assert resp.status_code == 200
        assert resp.json()["onboarding_complete"] is True
        assert commcare_called, "CommCare resolution was not called"
        assert connect_called, "Connect resolution was SKIPPED — this is the bug"
