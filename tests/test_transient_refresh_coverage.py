"""A transient OAuth refresh failure is not a coverage gap (#551 review M1).

With the all-of switch on, a refresh the provider *refused* (invalid_grant) leaves
the member uncovered with a reconnect remedy. A refresh that merely could not
complete (408/429/5xx, timeout, network error) says nothing about the grant, so
local coverage stays on the last-known-good credential; whether the provider is
reachable right now is the freshness gate's question, answered with a retryable
denial that keeps memberships.
"""

from datetime import timedelta

import httpx
import pytest
from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership, UpstreamAccessProof
from apps.users.services.token_refresh import (
    TokenRefreshRejected,
    TokenRefreshUnavailable,
    refresh_oauth_token,
)
from apps.workspaces.access import (
    TENANT_ACCESS_LOST,
    VERIFICATION_UNAVAILABLE,
    access_denied_body,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.workspaces.services.credential_coverage import (
    CoverageRecovery,
    CredentialGapCode,
    get_tenant_credential_readiness,
)
from tests.tenant_access import grant_ocs_team_access, ocs_team_connection

URL = "https://ocs.example/o/token/"

TRANSIENT = [
    pytest.param(lambda mock: mock.add_response(status_code=503, json={}), id="503"),
    pytest.param(lambda mock: mock.add_response(status_code=429, json={}), id="429"),
    pytest.param(lambda mock: mock.add_response(status_code=408, json={}), id="408"),
    pytest.param(lambda mock: mock.add_exception(httpx.ReadTimeout("slow")), id="timeout"),
    pytest.param(lambda mock: mock.add_exception(httpx.ConnectError("down")), id="network"),
]


@pytest.fixture
def ocs_member(user):
    tenant = Tenant.objects.create(provider="ocs", external_id="bot-1", canonical_name="Bot One")
    connection = ocs_team_connection(user, "acme")
    grant_ocs_team_access(user, tenant, connection)
    workspace = Workspace.objects.create(name="Bots", created_by=user)
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.READ)
    token = SocialToken.objects.select_related("app").get(account=connection.social_account)
    return workspace, tenant, token


def _readiness(user, tenant):
    (item,) = get_tenant_credential_readiness([(user.pk, tenant)])
    return item


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("fail", TRANSIENT)
async def test_transient_refresh_failure_keeps_coverage(user, ocs_member, fail, httpx_mock):
    _workspace, tenant, token = ocs_member
    fail(httpx_mock)

    with pytest.raises(TokenRefreshUnavailable):
        await refresh_oauth_token(token, URL)

    item = await sync_to_async(_readiness)(user, tenant)
    assert item.usable, item.gap


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("fail", TRANSIENT)
async def test_transient_refresh_failure_does_not_deny_the_workspace(
    user, ocs_member, fail, httpx_mock
):
    workspace, _tenant, token = ocs_member
    fail(httpx_mock)

    with pytest.raises(TokenRefreshUnavailable):
        await refresh_oauth_token(token, URL)

    result = await sync_to_async(resolve_workspace_access_ex)(user, workspace.id)
    assert result.granted, access_denied_body(result)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rejected_refresh_is_a_reconnect_gap(user, ocs_member, httpx_mock):
    _workspace, tenant, token = ocs_member
    httpx_mock.add_response(status_code=400, json={"error": "invalid_grant"})

    with pytest.raises(TokenRefreshRejected):
        await refresh_oauth_token(token, URL)

    item = await sync_to_async(_readiness)(user, tenant)
    assert item.gap.code == CredentialGapCode.OAUTH_REFRESH_FAILED


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rejected_refresh_denies_the_workspace_with_reconnect(user, ocs_member, httpx_mock):
    workspace, _tenant, token = ocs_member
    httpx_mock.add_response(status_code=400, json={"error": "invalid_grant"})

    with pytest.raises(TokenRefreshRejected):
        await refresh_oauth_token(token, URL)

    result = await sync_to_async(resolve_workspace_access_ex)(user, workspace.id)
    assert result.denied_reason == TENANT_ACCESS_LOST
    assert [t.recovery for t in result.missing_tenants] == [CoverageRecovery.RECONNECT]
    body = access_denied_body(result)
    assert body["missing_tenants"][0]["remedy"].startswith("reconnect ")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_provider_outage_is_a_retryable_freshness_denial_not_a_reconnect(
    user, ocs_member, httpx_mock
):
    """Where a live provider is needed, a blip still denies, but temporarily (#548)."""
    workspace, tenant, token = ocs_member
    await SocialToken.objects.filter(pk=token.pk).aupdate(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    await UpstreamAccessProof.objects.filter(tenant=tenant).aupdate(
        verified_at=timezone.now() - timedelta(days=1)
    )
    httpx_mock.add_response(status_code=503, json={}, is_reusable=True)

    result = await sync_to_async(resolve_workspace_access_ex)(user, workspace.id)

    assert result.denied_reason == VERIFICATION_UNAVAILABLE
    body = access_denied_body(result)
    assert body["retryable"] is True
    assert "retry" in body["error"]
    assert "reconnect" not in body["error"].lower()
    assert "missing_tenants" not in body
    assert await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()
    assert (await sync_to_async(_readiness)(user, tenant)).usable
