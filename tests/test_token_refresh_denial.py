"""Only authoritative OAuth refresh rejection revokes connection memberships."""

import httpx
import pytest
import requests
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.common.errors import UpstreamTokenExpired
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.token_refresh import (
    TokenRefreshError,
    refresh_oauth_token,
    refresh_oauth_token_sync,
)

URL = "https://connect.example/o/token/"


@pytest.fixture
def identities(user):
    app = SocialApp.objects.create(provider="ocs", name="Connect", client_id="id", secret="secret")
    result = []
    for index in range(2):
        account = SocialAccount.objects.create(
            user=user, provider="ocs", uid=f"user#{index}", extra_data={"team": str(index)}
        )
        token = SocialToken.objects.create(
            account=account, app=app, token=f"access-{index}", token_secret=f"refresh-{index}"
        )
        connection = TenantConnection.objects.create(
            user=user,
            provider="ocs",
            credential_type="oauth",
            scope_key=str(index),
            social_account=account,
        )
        tenant = Tenant.objects.create(
            provider="ocs", external_id=str(index), canonical_name=str(index)
        )
        member = TenantMembership.all_objects.create(
            user=user, tenant=tenant, connection=connection
        )
        result.append((token, connection, member))
    return result


async def invoke(
    mode, token, httpx_mock, requests_mock, *, status=400, payload=None, simulate_timeout=False
):
    if mode == "async":
        if simulate_timeout:
            httpx_mock.add_exception(httpx.ReadTimeout("timeout"))
        else:
            httpx_mock.add_response(status_code=status, json=payload)
        return await refresh_oauth_token(token, URL)
    if simulate_timeout:
        requests_mock.post(URL, exc=requests.Timeout("timeout"))
    else:
        requests_mock.post(URL, status_code=status, json=payload)
    return await sync_to_async(refresh_oauth_token_sync)(token, URL)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_invalid_grant_revokes_only_current_connection(
    identities, mode, status, httpx_mock, requests_mock
):
    token, connection, member = identities[0]
    with pytest.raises(TokenRefreshError) as caught:
        await invoke(
            mode,
            token,
            httpx_mock,
            requests_mock,
            status=status,
            payload={"error": "invalid_grant"},
        )
    assert isinstance(caught.value, UpstreamTokenExpired)
    assert not await TenantMembership.objects.filter(pk=member.pk).aexists()
    assert await TenantMembership.objects.filter(pk=identities[1][2].pk).aexists()
    await connection.arefresh_from_db()
    assert connection.upstream_denial_code == ErrorCode.AUTH_TOKEN_EXPIRED


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "status,payload,simulate_timeout",
    [
        (400, {"error": "invalid_client"}, False),
        (403, {}, False),
        (429, {"error": "invalid_grant"}, False),
        (503, {"error": "invalid_grant"}, False),
        (400, {}, True),
    ],
)
async def test_inconclusive_refresh_preserves_access(
    identities, mode, status, payload, simulate_timeout, httpx_mock, requests_mock
):
    token, connection, member = identities[0]
    with pytest.raises(TokenRefreshError) as caught:
        await invoke(
            mode,
            token,
            httpx_mock,
            requests_mock,
            status=status,
            payload=payload,
            simulate_timeout=simulate_timeout,
        )
    assert not isinstance(caught.value, UpstreamTokenExpired)
    assert await TenantMembership.objects.filter(pk=member.pk).aexists()
    await connection.arefresh_from_db()
    assert connection.upstream_denial_code == ""
    assert connection.oauth_refresh_failure_fingerprint


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_old_refresh_denial_does_not_revoke_replacement(
    identities, mode, httpx_mock, requests_mock
):
    token, connection, member = identities[0]
    await SocialToken.objects.filter(pk=token.pk).aupdate(
        token="replacement", token_secret="replacement-refresh"
    )
    with pytest.raises(TokenRefreshError):
        await invoke(mode, token, httpx_mock, requests_mock, payload={"error": "invalid_grant"})
    assert await TenantMembership.objects.filter(pk=member.pk).aexists()
    await connection.arefresh_from_db()
    assert connection.upstream_denial_code == ""
    assert connection.oauth_refresh_failure_fingerprint == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_successful_refresh_does_not_restore_denied_memberships(
    identities, mode, httpx_mock, requests_mock
):

    token, connection, member = identities[0]
    await TenantMembership.all_objects.filter(pk=member.pk).aupdate(archived_at=timezone.now())
    await TenantConnection.objects.filter(pk=connection.pk).aupdate(
        upstream_denial_code=ErrorCode.AUTH_TOKEN_EXPIRED, upstream_denied_at=timezone.now()
    )
    assert (
        await invoke(
            mode, token, httpx_mock, requests_mock, status=200, payload={"access_token": "renewed"}
        )
        == "renewed"
    )
    assert not await TenantMembership.objects.filter(pk=member.pk).aexists()
    await connection.arefresh_from_db()
    assert connection.upstream_denial_code == ErrorCode.AUTH_TOKEN_EXPIRED
