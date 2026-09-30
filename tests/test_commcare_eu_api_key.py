"""A CommCare API key names its HQ server, and every call it makes goes there (#719)."""

import pytest
from django.test import Client

from apps.users.adapters import encrypt_credential
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.api_key_providers import CredentialVerificationError
from apps.users.services.api_key_providers.commcare import CommCareStrategy

EU_DOMAINS = "https://eu.commcarehq.org/api/user_domains/v1/"
FIELDS = {"domain": "dom", "username": "u@example.com", "api_key": "k"}


def _client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db(transaction=True)
def test_eu_api_key_is_verified_at_eu_and_stored_on_the_eu_server(user, httpx_mock):
    httpx_mock.add_response(url=EU_DOMAINS, json={"objects": [{"domain_name": "dom"}]})

    response = _client(user).post(
        "/api/auth/connections/",
        data={"provider": "commcare", "fields": {**FIELDS, "server": "eu"}},
        content_type="application/json",
    )

    assert response.status_code == 201
    assert httpx_mock.get_request().headers["Authorization"] == "ApiKey u@example.com:k"
    tenant = Tenant.objects.get(external_id="dom")
    assert (tenant.provider, tenant.server) == ("commcare", "eu")
    conn = TenantConnection.objects.get(user=user)
    assert (conn.credential_type, conn.scope_key, conn.scope_label) == ("api_key", "eu", "EU")
    assert TenantMembership.objects.get(user=user).connection_id == conn.id


@pytest.mark.django_db(transaction=True)
def test_eu_key_does_not_join_a_www_tenant_with_the_same_domain(user, httpx_mock):
    www = Tenant.objects.create(provider="commcare", external_id="dom")
    httpx_mock.add_response(url=EU_DOMAINS, json={"objects": [{"domain_name": "dom"}]})

    response = _client(user).post(
        "/api/auth/connections/",
        data={"provider": "commcare", "fields": {**FIELDS, "server": "eu"}},
        content_type="application/json",
    )

    assert response.status_code == 201
    assert not TenantMembership.objects.filter(user=user, tenant=www).exists()
    assert TenantMembership.objects.filter(user=user, tenant__server="eu").exists()


@pytest.mark.django_db
def test_unknown_server_is_rejected_without_calling_upstream(user, httpx_mock):
    response = _client(user).post(
        "/api/auth/connections/",
        data={"provider": "commcare", "fields": {**FIELDS, "server": "mars"}},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "Unknown CommCare HQ server" in response.json()["error"]
    assert not httpx_mock.get_requests()
    assert not TenantConnection.objects.filter(user=user).exists()


@pytest.mark.django_db(transaction=True)
def test_rotating_an_eu_key_verifies_it_at_eu(user, httpx_mock):
    tenant = Tenant.objects.create(provider="commcare", server="eu", external_id="dom")
    conn = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("u@example.com:old"),
        scope_key="eu",
    )
    TenantMembership.objects.create(user=user, tenant=tenant, connection=conn)
    httpx_mock.add_response(url=EU_DOMAINS, json={"objects": [{"domain_name": "dom"}]})

    response = _client(user).patch(
        f"/api/auth/connections/{conn.id}/",
        data={"fields": {"username": "u@example.com", "api_key": "new"}},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert str(httpx_mock.get_request().url) == EU_DOMAINS


def test_the_add_form_offers_each_server_defaulting_to_www():
    server = next(f for f in CommCareStrategy.form_fields if f["key"] == "server")
    assert server["type"] == "select"
    assert not server["editable_on_rotate"]
    assert [o["value"] for o in server["options"]] == ["", "eu"]
    assert CommCareStrategy.server_for(FIELDS) == ""


@pytest.mark.asyncio
async def test_a_domain_on_a_later_page_is_found(httpx_mock):
    httpx_mock.add_response(
        url=EU_DOMAINS,
        json={
            "objects": [{"domain_name": "other"}],
            "meta": {"next": "/api/user_domains/v1/?offset=1"},
        },
    )
    httpx_mock.add_response(
        url=f"{EU_DOMAINS}?offset=1",
        json={"objects": [{"domain_name": "dom"}], "meta": {"next": None}},
    )

    descriptors = await CommCareStrategy.verify_and_discover({**FIELDS, "server": "eu"})

    assert [d.external_id for d in descriptors] == ["dom"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"json": {"meta": {"next": None}}},
        {"text": "<html>maintenance</html>"},
        {"json": {"objects": [], "meta": {"next": "https://evil.example/api/"}}},
    ],
)
async def test_an_unexpected_domain_list_is_a_rejection(httpx_mock, response):
    httpx_mock.add_response(url=EU_DOMAINS, **response)

    with pytest.raises(CredentialVerificationError, match="unexpected domain list"):
        await CommCareStrategy.verify_and_discover({**FIELDS, "server": "eu"})
