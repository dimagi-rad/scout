"""Signing in with CommCare HQ (EU) is its own OAuth provider, hidden until configured (#719)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin, SocialToken
from allauth.socialaccount.providers import registry
from django.contrib.sites.models import Site
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.users.adapters import EncryptingSocialAccountAdapter
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.providers.commcare.views import CommCareOAuth2Adapter
from apps.users.providers.commcare_eu.views import CommCareEUOAuth2Adapter
from apps.users.signals import resolve_tenant_on_social_login
from tests.test_oauth_domain_restriction import _make_request, _make_sociallogin

EU = "https://eu.commcarehq.org"


@pytest.fixture
def site(db):
    site, _ = Site.objects.get_or_create(id=1, defaults={"domain": "testserver", "name": "Test"})
    return site


def _app(site, provider, name):
    app = SocialApp.objects.create(provider=provider, name=name, client_id="id", secret="s")
    app.sites.add(site)
    return app


def _identity(user, app, provider, *, token, expires_at=None):
    account = SocialAccount.objects.create(user=user, provider=provider, uid=f"{provider}-1")
    SocialToken.objects.create(
        account=account,
        app=app,
        token=token,
        token_secret="refresh",
        expires_at=expires_at or timezone.now() + timedelta(hours=1),
    )
    return account


def test_eu_provider_talks_to_eu_hq():
    assert registry.get_class("commcare_eu").name == "CommCare HQ (EU)"
    assert CommCareEUOAuth2Adapter.authorize_url == f"{EU}/oauth/authorize/"
    assert CommCareEUOAuth2Adapter.access_token_url == f"{EU}/oauth/token/"
    assert CommCareEUOAuth2Adapter.profile_url == f"{EU}/api/v0.5/identity/"
    assert CommCareOAuth2Adapter.authorize_url == "https://www.commcarehq.org/oauth/authorize/"
    assert reverse("commcare_eu_login") == "/accounts/commcare_eu/login/"


@pytest.mark.django_db
class TestConfiguration:
    def test_setup_skips_eu_until_its_credentials_are_set(self, monkeypatch):
        monkeypatch.delenv("COMMCARE_EU_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("COMMCARE_EU_OAUTH_CLIENT_SECRET", raising=False)
        call_command("setup_oauth_apps")
        assert not SocialApp.objects.filter(provider="commcare_eu").exists()

    def test_setup_registers_eu_from_its_own_env_vars(self, monkeypatch):
        monkeypatch.setenv("COMMCARE_EU_OAUTH_CLIENT_ID", "eu-id")
        monkeypatch.setenv("COMMCARE_EU_OAUTH_CLIENT_SECRET", "eu-secret")
        call_command("setup_oauth_apps")
        app = SocialApp.objects.get(provider="commcare_eu")
        assert (app.name, app.client_id, app.secret) == ("CommCare HQ (EU)", "eu-id", "eu-secret")

    def test_eu_is_hidden_from_sign_in_options_when_unconfigured(self, site):
        _app(site, "commcare", "CommCare HQ")
        ids = [p["id"] for p in Client().get("/api/auth/providers/").json()["providers"]]
        assert ids == ["commcare"]

    def test_eu_is_offered_once_configured(self, site):
        _app(site, "commcare", "CommCare HQ")
        _app(site, "commcare_eu", "CommCare HQ (EU)")
        providers = {p["id"]: p for p in Client().get("/api/auth/providers/").json()["providers"]}
        assert providers["commcare_eu"]["login_url"] == "/accounts/commcare_eu/login/"
        assert providers["commcare_eu"]["name"] == "CommCare HQ (EU)"


@pytest.mark.django_db(transaction=True)
class TestSignIn:
    def test_eu_sign_in_discovers_eu_domains(self, user, site, httpx_mock):
        app = _app(site, "commcare_eu", "CommCare HQ (EU)")
        account = _identity(user, app, "commcare_eu", token="eu-access")
        httpx_mock.add_response(
            url=f"{EU}/api/user_domains/v1/",
            json={"meta": {"next": None}, "objects": [{"domain_name": "dom"}]},
        )
        sociallogin = SocialLogin(
            user=user, account=account, token=SocialToken.objects.get(account=account)
        )

        resolve_tenant_on_social_login(None, sociallogin)

        membership = TenantMembership.objects.select_related("tenant", "connection").get(user=user)
        assert (membership.tenant.server, membership.tenant.external_id) == ("eu", "dom")
        assert membership.connection.scope_key == "eu"

    def test_expired_eu_token_is_renewed_at_eu_when_listing_providers(self, user, site, httpx_mock):
        app = _app(site, "commcare_eu", "CommCare HQ (EU)")
        _identity(
            user,
            app,
            "commcare_eu",
            token="old",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{EU}/oauth/token/",
            json={"access_token": "new", "refresh_token": "r2", "expires_in": 900},
        )
        client = Client()
        client.force_login(user)

        providers = {p["id"]: p for p in client.get("/api/auth/providers/").json()["providers"]}

        assert providers["commcare_eu"]["status"] == "connected"
        assert str(httpx_mock.get_request().url) == f"{EU}/oauth/token/"

    def test_disconnecting_eu_leaves_www_connected(self, user, site):
        www_app = _app(site, "commcare", "CommCare HQ")
        eu_app = _app(site, "commcare_eu", "CommCare HQ (EU)")
        www = _identity(user, www_app, "commcare", token="www-access")
        eu = _identity(user, eu_app, "commcare_eu", token="eu-access")
        www_conn = TenantConnection.objects.create(
            user=user, provider="commcare", credential_type="oauth", social_account=www
        )
        eu_conn = TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type="oauth",
            scope_key="eu",
            social_account=eu,
        )
        eu_tenant = Tenant.objects.create(provider="commcare", server="eu", external_id="dom")
        www_tenant = Tenant.objects.create(provider="commcare", external_id="dom")
        TenantMembership.objects.create(user=user, tenant=eu_tenant, connection=eu_conn)
        TenantMembership.objects.create(user=user, tenant=www_tenant, connection=www_conn)
        client = Client()
        client.force_login(user)

        response = client.post("/api/auth/providers/commcare_eu/disconnect/")

        assert response.status_code == 200
        assert SocialToken.objects.filter(account=www).exists()
        assert not SocialToken.objects.filter(account=eu).exists()
        assert TenantConnection.objects.filter(pk=www_conn.pk).exists()
        assert not TenantConnection.objects.filter(pk=eu_conn.pk).exists()
        assert TenantMembership.objects.filter(tenant=www_tenant).exists()
        assert not TenantMembership.objects.filter(tenant=eu_tenant).exists()

    def test_disconnecting_www_leaves_eu_connected(self, user, site):
        SocialApp.objects.create(provider="commcare", provider_id="hq_production", name="HQ")
        eu_app = _app(site, "commcare_eu", "CommCare HQ (EU)")
        eu = _identity(user, eu_app, "commcare_eu", token="eu-access")
        for provider in ("commcare", "hq_production"):
            account = SocialAccount.objects.create(user=user, provider=provider, uid=provider)
            SocialToken.objects.create(account=account, token=provider)
        eu_conn = TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type="oauth",
            scope_key="eu",
            social_account=eu,
        )
        client = Client()
        client.force_login(user)

        assert client.post("/api/auth/providers/commcare/disconnect/").status_code == 200

        assert list(
            SocialToken.objects.filter(account__user=user).values_list("token", flat=True)
        ) == ["eu-access"]
        assert TenantConnection.objects.filter(pk=eu_conn.pk).exists()


class TestSignInServerGuard:
    @staticmethod
    def _login(adapter_id, stored_id):
        login = _make_sociallogin(stored_id, "a@dimagi.com")
        login.provider = SimpleNamespace(id=adapter_id)
        return login

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={})
    def test_the_eu_provider_accepts_eu_ids(self):
        login = self._login("commcare_eu", "commcare_eu")
        assert EncryptingSocialAccountAdapter().pre_social_login(_make_request(), login) is None

    @override_settings(SOCIALACCOUNT_ALLOWED_EMAIL_DOMAINS={})
    def test_the_eu_provider_refuses_a_www_id(self):
        with pytest.raises(ImmediateHttpResponse):
            EncryptingSocialAccountAdapter().pre_social_login(
                _make_request(), self._login("commcare_eu", "commcare_prod")
            )
