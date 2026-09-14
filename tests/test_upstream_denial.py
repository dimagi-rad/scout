"""Durable denial scope, non-revocation on inconclusive responses, and recovery."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from apps.common.error_codes import ErrorCode
from apps.common.errors import ConnectAuthError, OCSAuthError
from apps.users.models import Tenant, TenantConnection, TenantMembership, User
from apps.users.services.tenant_resolution import (
    _aoauth_connection,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.users.services.upstream_denial import arecord_upstream_denial


async def identity(user, provider="commcare_connect", scope=""):
    account = await SocialAccount.objects.acreate(
        user=user, provider=provider, uid=f"user#{scope}", extra_data={"team": scope}
    )
    token = await SocialToken.objects.acreate(account=account, token=f"token-{provider}-{scope}")
    conn = await TenantConnection.objects.acreate(
        user=user,
        provider=provider,
        credential_type="oauth",
        scope_key=scope,
        social_account=account,
    )
    tenant = await Tenant.objects.acreate(
        provider=provider, external_id=scope or "1", canonical_name="A"
    )
    membership = await TenantMembership.all_objects.acreate(
        user=user, tenant=tenant, connection=conn
    )
    return account, token, conn, membership


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", [401, 403])
async def test_discovery_denial_archives_only_observed_connection(user, httpx_mock, status):
    account, token, conn, tm = await identity(user)
    _, _, _other, sibling = await identity(user, "ocs", "other-team")
    httpx_mock.add_response(status_code=status)
    with pytest.raises(ConnectAuthError):
        await resolve_connect_opportunities(user, token.token, social_account=account)
    await tm.arefresh_from_db()
    assert tm.archived_at is not None
    assert await TenantMembership.objects.filter(pk=sibling.pk).aexists()
    await conn.arefresh_from_db()
    assert conn.upstream_denial_code == (
        "AUTH_TOKEN_EXPIRED" if status == 401 else "AUTH_ACCESS_DENIED"
    )
    assert conn.upstream_denied_at is not None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_inconclusive_discovery_preserves_memberships(user, httpx_mock, status):
    account, token, _conn, tm = await identity(user)
    httpx_mock.add_response(status_code=status)
    with pytest.raises(httpx.HTTPStatusError):
        await resolve_connect_opportunities(user, token.token, social_account=account)
    assert await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_old_token_denial_does_not_revoke_replacement(user, httpx_mock):
    account, token, _conn, tm = await identity(user)
    old_token = token.token
    token.token = "replacement"
    await token.asave()
    httpx_mock.add_response(status_code=401)
    with pytest.raises(ConnectAuthError):
        await resolve_connect_opportunities(user, old_token, social_account=account)
    assert await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_successful_discovery_restores_access_and_clears_denial(user, httpx_mock):
    account, token, conn, tm = await identity(user)
    httpx_mock.add_response(status_code=401)
    with pytest.raises(ConnectAuthError):
        await resolve_connect_opportunities(user, token.token, social_account=account)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()
    await conn.arefresh_from_db()
    denied_at = conn.upstream_denied_at
    token.token = "reconnected"
    await token.asave()
    httpx_mock.add_response(json={"opportunities": [{"id": 1, "name": "A"}]})
    await resolve_connect_opportunities(user, token.token, social_account=account)
    assert await TenantMembership.objects.filter(pk=tm.pk).aexists()
    await conn.arefresh_from_db()
    assert conn.upstream_denial_code == ""
    assert conn.upstream_denied_at == denied_at


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ocs_team_denial_does_not_revoke_other_team(user, httpx_mock):
    account, token, _conn, tm = await identity(user, "ocs", "team-a")
    _, _, _, sibling = await identity(user, "ocs", "team-b")
    httpx_mock.add_response(status_code=403)
    with patch(
        "apps.users.services.tenant_resolution.adetect_team_name_from_oauth",
        new=AsyncMock(return_value="A"),
    ):
        with pytest.raises(OCSAuthError):
            await resolve_ocs_chatbots(user, token.token, social_account=account)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()
    assert await TenantMembership.objects.filter(pk=sibling.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stale_success_does_not_restore_denied_replacement(user, httpx_mock):
    account, token, _conn, tm = await identity(user)
    httpx_mock.add_response(status_code=401)
    with pytest.raises(ConnectAuthError):
        await resolve_connect_opportunities(user, token.token, social_account=account)
    old_token = token.token
    token.token = "replacement"
    await token.asave()
    httpx_mock.add_response(json={"opportunities": [{"id": 1, "name": "A"}]})
    await resolve_connect_opportunities(user, old_token, social_account=account)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_discovery_started_before_tenant_denial_cannot_restore_it(user, httpx_mock):

    account, token, conn, tm = await identity(user)

    async def response(request):
        await arecord_upstream_denial(
            conn, credential=token.token, code=ErrorCode.AUTH_ACCESS_DENIED, tenant_id=tm.tenant_id
        )
        return httpx.Response(200, json={"opportunities": [{"id": 1, "name": "A"}]})

    httpx_mock.add_callback(response)
    await resolve_connect_opportunities(user, token.token, social_account=account)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stale_discovery_cannot_restore_denial_after_newer_discovery(user, httpx_mock):
    account, token, conn, tm = await identity(user)

    async def stale_response(request):
        await arecord_upstream_denial(
            conn, credential=token.token, code=ErrorCode.AUTH_ACCESS_DENIED, tenant_id=tm.tenant_id
        )
        await resolve_connect_opportunities(user, token.token, social_account=account)
        assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()
        return httpx.Response(200, json={"opportunities": [{"id": 1, "name": "A"}]})

    httpx_mock.add_callback(stale_response)
    httpx_mock.add_response(json={"opportunities": []})
    await resolve_connect_opportunities(user, token.token, social_account=account)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_denial_between_new_identity_binding_and_sync_wins(user, httpx_mock):

    _account, _token, _conn, tm = await identity(user)
    replacement = await SocialAccount.objects.acreate(
        user=user, provider="commcare_connect", uid="replacement"
    )
    await SocialToken.objects.acreate(account=replacement, token="replacement")

    async def bind_then_deny(*args, **kwargs):
        bound = await _aoauth_connection(*args, **kwargs)
        await arecord_upstream_denial(
            bound, credential="replacement", code=ErrorCode.AUTH_TOKEN_EXPIRED
        )
        return bound

    httpx_mock.add_response(json={"opportunities": [{"id": 1, "name": "A"}]})
    with patch(
        "apps.users.services.tenant_resolution._aoauth_connection", side_effect=bind_then_deny
    ):
        await resolve_connect_opportunities(user, "replacement", social_account=replacement)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ocs_denial_preserves_legacy_other_team_rows(user, httpx_mock):
    account, token, conn, _tm = await identity(user, "ocs", "team-a")
    other_tenant = await Tenant.objects.acreate(
        provider="ocs", external_id="legacy-b", canonical_name="B"
    )
    sibling = await TenantMembership.all_objects.acreate(
        user=user, tenant=other_tenant, connection=conn, provider_metadata={"team_slug": "team-b"}
    )
    httpx_mock.add_response(status_code=401)
    with patch(
        "apps.users.services.tenant_resolution.adetect_team_name_from_oauth",
        new=AsyncMock(return_value="A"),
    ):
        with pytest.raises(OCSAuthError):
            await resolve_ocs_chatbots(user, token.token, social_account=account)
    assert await TenantMembership.objects.filter(pk=sibling.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_new_identity_token_replaced_before_sync_is_not_restored(user, httpx_mock):
    _account, token, conn, tm = await identity(user)
    await arecord_upstream_denial(conn, credential=token.token, code=ErrorCode.AUTH_TOKEN_EXPIRED)
    replacement = await SocialAccount.objects.acreate(
        user=user, provider="commcare_connect", uid="replacement"
    )
    new_token = await SocialToken.objects.acreate(account=replacement, token="replacement")

    async def bind_then_replace_token(*args, **kwargs):
        bound = await _aoauth_connection(*args, **kwargs)
        await SocialToken.objects.filter(pk=new_token.pk).aupdate(token="newer")
        return bound

    httpx_mock.add_response(json={"opportunities": [{"id": 1, "name": "A"}]})
    with patch(
        "apps.users.services.tenant_resolution._aoauth_connection",
        side_effect=bind_then_replace_token,
    ):
        await resolve_connect_opportunities(user, "replacement", social_account=replacement)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_late_refresh_rejection_cannot_revoke_changed_refresh_secret(user):
    _account, token, conn, tm = await identity(user)
    snapshot = (token.pk, token.token_secret, token.app_id)
    await SocialToken.objects.filter(pk=token.pk).aupdate(token_secret="new-refresh-secret")
    await arecord_upstream_denial(
        conn, credential=token.token, code=ErrorCode.AUTH_TOKEN_EXPIRED, token_snapshot=snapshot
    )
    assert await TenantMembership.objects.filter(pk=tm.pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_denial_preserves_another_users_membership_on_same_tenant(user):

    _account, token, conn, tm = await identity(user)
    other_user = await User.objects.acreate(email="other@example.com")
    other_conn = await TenantConnection.objects.acreate(
        user=other_user, provider="commcare_connect", credential_type="api_key"
    )
    other = await TenantMembership.all_objects.acreate(
        user=other_user, tenant_id=tm.tenant_id, connection=other_conn
    )
    await arecord_upstream_denial(conn, credential=token.token, code=ErrorCode.AUTH_TOKEN_EXPIRED)
    assert not await TenantMembership.objects.filter(pk=tm.pk).aexists()
    assert await TenantMembership.objects.filter(pk=other.pk).aexists()
