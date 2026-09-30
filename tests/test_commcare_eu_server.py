"""EU CommCare HQ: a tenant's server decides every URL Scout sends its credential to (#719)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests_mock
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.db import IntegrityError
from django.utils import timezone

from apps.common.commcare_servers import UnknownCommCareServer, server_for_provider
from apps.common.identifiers import refresh_schema_name, tenant_schema_name
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.providers.commcare.views import CommCareOAuth2Adapter
from apps.users.services.access_verification_providers import verify_provider
from apps.users.services.access_verification_types import (
    CredentialObservation,
    CredentialRequestSnapshot,
    VerificationOutcome,
)
from apps.users.services.credential_resolver import aresolve_credential
from apps.users.services.oauth_scope import account_scope
from apps.users.services.tenant_resolution import resolve_commcare_domains
from apps.users.services.token_refresh import get_token_url
from mcp_server.loaders.commcare_cases import CommCareCaseLoader
from mcp_server.loaders.commcare_forms import CommCareFormLoader
from mcp_server.loaders.commcare_metadata import CommCareMetadataLoader
from mcp_server.services.materializer import _run_discover_phase

EU = "https://eu.commcarehq.org"
WWW = "https://www.commcarehq.org"
CREDENTIAL = {"type": "oauth", "value": "secret"}


def _domains(*names):
    return {
        "meta": {"next": None},
        "objects": [{"domain_name": name, "project_name": name.title()} for name in names],
    }


def _eu_identity(user, *, token="eu-access", expires_at=None):
    app = SocialApp.objects.create(
        provider="commcare_eu", name="CommCare HQ (EU)", client_id="eu-client", secret="eu-secret"
    )
    account = SocialAccount.objects.create(user=user, provider="commcare_eu", uid="eu-1")
    SocialToken.objects.create(
        account=account,
        app=app,
        token=token,
        token_secret="eu-refresh",
        expires_at=expires_at or timezone.now() + timedelta(hours=1),
    )
    return account


_aeu_identity = sync_to_async(_eu_identity)


class TestLoaders:
    def test_case_loader_reads_the_eu_server(self):
        with requests_mock.Mocker() as mock:
            mock.get(f"{EU}/a/dom/api/case/v2/", json={"cases": [{"case_id": "c"}], "next": None})
            cases = CommCareCaseLoader("dom", CREDENTIAL, server="eu").load()
        assert [c["case_id"] for c in cases] == ["c"]
        assert mock.last_request.url.startswith(f"{EU}/a/dom/api/case/v2/")

    def test_form_loader_reads_the_eu_server(self):
        with requests_mock.Mocker() as mock:
            mock.get(f"{EU}/a/dom/api/v0.5/form/", json={"objects": [], "meta": {"next": None}})
            assert CommCareFormLoader("dom", CREDENTIAL, server="eu").load() == []
        assert mock.last_request.url.startswith(f"{EU}/a/dom/api/v0.5/form/")

    def test_metadata_loader_reads_the_eu_server(self):
        with requests_mock.Mocker() as mock:
            mock.get(
                f"{EU}/a/dom/api/v0.5/application/", json={"objects": [], "meta": {"next": None}}
            )
            CommCareMetadataLoader("dom", CREDENTIAL, server="eu").load()
        assert mock.last_request.url.startswith(f"{EU}/a/dom/api/v0.5/application/")

    def test_eu_loader_never_follows_a_next_link_to_www(self):
        with requests_mock.Mocker() as mock:
            mock.get(
                f"{EU}/a/dom/api/case/v2/",
                json={"cases": [], "next": f"{WWW}/a/dom/api/case/v2/?cursor=x"},
            )
            with pytest.raises(ValueError, match="origin"):
                CommCareCaseLoader("dom", CREDENTIAL, server="eu").load()
        assert mock.call_count == 1

    def test_default_loader_still_reads_www(self):
        with requests_mock.Mocker() as mock:
            mock.get(f"{WWW}/a/dom/api/case/v2/", json={"cases": [], "next": None})
            CommCareCaseLoader("dom", CREDENTIAL).load()
        assert mock.last_request.url.startswith(f"{WWW}/a/dom/api/case/v2/")

    def test_unknown_server_is_refused_rather_than_sent_to_www(self):
        with pytest.raises(UnknownCommCareServer):
            CommCareCaseLoader("dom", CREDENTIAL, server="mars")

    @pytest.mark.django_db
    def test_discovery_phase_uses_the_tenant_server(self):
        tenant = Tenant.objects.create(provider="commcare", server="eu", external_id="dom")
        pipeline = SimpleNamespace(has_metadata_discovery=True, provider="commcare")
        with requests_mock.Mocker() as mock:
            mock.get(
                f"{EU}/a/dom/api/v0.5/application/", json={"objects": [], "meta": {"next": None}}
            )
            _run_discover_phase(SimpleNamespace(tenant=tenant), CREDENTIAL, pipeline)
        assert mock.last_request.url.startswith(f"{EU}/")


@pytest.mark.django_db(transaction=True)
class TestDiscovery:
    @pytest.mark.asyncio
    async def test_eu_identity_discovers_eu_domains(self, user, httpx_mock):
        account = await _aeu_identity(user)
        httpx_mock.add_response(url=f"{EU}/api/user_domains/v1/", json=_domains("dom"))

        memberships = await resolve_commcare_domains(user, "eu-access", social_account=account)

        tenant = memberships[0].tenant
        assert (tenant.provider, tenant.server, tenant.external_id) == ("commcare", "eu", "dom")
        conn = await TenantConnection.objects.aget(user=user, provider="commcare")
        assert (conn.scope_key, conn.social_account_id) == ("eu", account.pk)

    @pytest.mark.asyncio
    async def test_same_domain_on_www_and_eu_stays_two_tenants(self, user, httpx_mock):
        www = await Tenant.objects.acreate(provider="commcare", external_id="dom")
        account = await _aeu_identity(user)
        httpx_mock.add_response(url=f"{EU}/api/user_domains/v1/", json=_domains("dom"))

        memberships = await resolve_commcare_domains(user, "eu-access", social_account=account)

        assert memberships[0].tenant_id != www.pk
        assert await Tenant.objects.filter(provider="commcare", external_id="dom").acount() == 2

    @pytest.mark.asyncio
    async def test_eu_and_www_connections_coexist(self, user, httpx_mock):
        www_account = await SocialAccount.objects.acreate(
            user=user, provider="commcare", uid="www-1"
        )
        await SocialToken.objects.acreate(account=www_account, token="www-access")
        eu_account = await _aeu_identity(user)
        httpx_mock.add_response(url=f"{WWW}/api/user_domains/v1/", json=_domains("w"))
        httpx_mock.add_response(url=f"{EU}/api/user_domains/v1/", json=_domains("e"))

        await resolve_commcare_domains(user, "www-access", social_account=www_account)
        await resolve_commcare_domains(user, "eu-access", social_account=eu_account)

        scopes = {
            c.scope_key: c.social_account_id
            async for c in TenantConnection.objects.filter(user=user, provider="commcare")
        }
        assert scopes == {"": www_account.pk, "eu": eu_account.pk}
        # Binding the EU identity must not retire the www identity's token.
        assert await SocialToken.objects.filter(account=www_account).aexists()
        live = {
            (m.tenant.server, m.tenant.external_id)
            async for m in TenantMembership.objects.filter(user=user).select_related("tenant")
        }
        assert live == {("", "w"), ("eu", "e")}


def test_tenant_identity_includes_server(db):
    Tenant.objects.create(provider="commcare", external_id="dom")
    Tenant.objects.create(provider="commcare", server="eu", external_id="dom")
    with pytest.raises(IntegrityError):
        Tenant.objects.create(provider="commcare", server="eu", external_id="dom")


class TestSchemaNames:
    def test_www_names_are_unchanged(self):
        assert tenant_schema_name("commcare", "dom") == tenant_schema_name("commcare", "dom", "")
        assert tenant_schema_name("commcare", "dom") == "dom_acacd32f"

    def test_eu_tenant_gets_its_own_schema(self):
        assert tenant_schema_name("commcare", "dom", "eu") != tenant_schema_name("commcare", "dom")
        assert refresh_schema_name("commcare", "dom", token="t", server="eu") != (
            refresh_schema_name("commcare", "dom", token="t")
        )


class TestTokenRefresh:
    def test_token_url_follows_the_server(self):
        assert get_token_url("commcare") == f"{WWW}/oauth/token/"
        assert get_token_url("commcare", "eu") == f"{EU}/oauth/token/"
        assert get_token_url("commcare", "mars") is None

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.asyncio
    async def test_expired_eu_token_refreshes_at_eu(self, user, httpx_mock):
        account = await _aeu_identity(user, expires_at=timezone.now() - timedelta(minutes=1))
        conn = await TenantConnection.objects.acreate(
            user=user,
            provider="commcare",
            credential_type=TenantConnection.OAUTH,
            scope_key="eu",
            social_account=account,
        )
        tenant = await Tenant.objects.acreate(provider="commcare", server="eu", external_id="dom")
        membership = await TenantMembership.objects.acreate(
            user=user, tenant=tenant, connection=conn
        )
        httpx_mock.add_response(
            method="POST",
            url=f"{EU}/oauth/token/",
            json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 900},
        )

        membership = await TenantMembership.objects.select_related("connection", "user").aget(
            pk=membership.pk
        )
        credential = await aresolve_credential(membership)

        assert credential["value"] == "fresh"
        request = httpx_mock.get_request()
        assert str(request.url) == f"{EU}/oauth/token/"
        assert b"client_id=eu-client" in request.content


def _observation(scope_key, credential_type=TenantConnection.OAUTH):
    return CredentialRequestSnapshot(
        CredentialObservation(
            connection_id=uuid4(),
            user_id=1,
            provider="commcare",
            credential_type=credential_type,
            credential_fingerprint="fingerprint",
            account_identity="account",
            scope_key=scope_key,
            upstream_denied_at=None,
        ),
        "user@example.com:key" if credential_type == TenantConnection.API_KEY else "secret",
    )


class TestAccessVerification:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("credential_type", [TenantConnection.OAUTH, TenantConnection.API_KEY])
    async def test_eu_connection_is_verified_against_eu(self, httpx_mock, credential_type):
        httpx_mock.add_response(url=f"{EU}/api/user_domains/v1/", json=_domains("dom"))

        result = await verify_provider(_observation("eu", credential_type))

        assert result.outcome == VerificationOutcome.COMPLETE
        assert result.external_ids == {"dom"}

    @pytest.mark.asyncio
    async def test_unknown_server_is_indeterminate(self):
        result = await verify_provider(_observation("mars"))
        assert result.outcome == VerificationOutcome.INDETERMINATE


@pytest.mark.parametrize(
    ("provider_id", "server"),
    [
        ("commcare", ""),
        ("commcare_prod", ""),
        ("hq_production", ""),
        ("commcare_eu", "eu"),
        ("commcare_eu_prod", "eu"),
    ],
)
def test_identity_server_follows_the_allauth_provider_id(provider_id, server):
    assert server_for_provider(provider_id) == server
    assert account_scope(SimpleNamespace(provider=provider_id, uid="u", extra_data={})) == (
        server if provider_id.startswith("commcare") else ""
    )


def test_www_sign_in_endpoints_are_unchanged():
    assert CommCareOAuth2Adapter.access_token_url == f"{WWW}/oauth/token/"
    assert CommCareOAuth2Adapter.authorize_url == f"{WWW}/oauth/authorize/"
    assert CommCareOAuth2Adapter.profile_url == f"{WWW}/api/v0.5/identity/"
