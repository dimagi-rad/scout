"""Full-sync resolution: grant (add/un-archive) + revocation (archive) with the
mass-revoke footguns the Fable review caught — CommCare pagination truncation,
OCS team scoping, and 2xx shape drift.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from allauth.socialaccount.models import SocialAccount
from django.utils import timezone

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.tenant_resolution import (
    COMMCARE_DOMAIN_API,
    TenantResolutionError,
    _fetch_all_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)

CONNECT_URL = "https://connect.dimagi.com/export/opp_org_program_list/"


async def _oauth_conn(user, provider):
    return await TenantConnection.objects.acreate(
        user=user, provider=provider, credential_type=TenantConnection.OAUTH
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_connect_full_sync_adds_unarchives_and_revokes(user, httpx_mock):
    conn = await _oauth_conn(user, "commcare_connect")
    t_keep = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="1", canonical_name="Keep"
    )
    t_gone = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="2", canonical_name="Gone"
    )
    t_back = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="3", canonical_name="Back"
    )
    await TenantMembership.all_objects.acreate(user=user, tenant=t_keep, connection=conn)
    await TenantMembership.all_objects.acreate(
        user=user, tenant=t_gone, connection=conn
    )  # will revoke
    await TenantMembership.all_objects.acreate(
        user=user, tenant=t_back, connection=conn, archived_at=timezone.now()
    )  # tombstone that returns

    httpx_mock.add_response(
        url=CONNECT_URL,
        json={"opportunities": [{"id": 1, "name": "Keep"}, {"id": 3, "name": "Back"}]},
    )
    await resolve_connect_opportunities(user, "tok")

    assert await TenantMembership.objects.filter(user=user, tenant=t_keep).aexists()  # stays live
    assert not await TenantMembership.objects.filter(
        user=user, tenant=t_gone
    ).aexists()  # revoked (hidden)
    assert await TenantMembership.all_objects.filter(
        user=user, tenant=t_gone, archived_at__isnull=False
    ).aexists()  # tombstoned, not deleted
    assert await TenantMembership.objects.filter(user=user, tenant=t_back).aexists()  # un-archived


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_connect_shape_drift_raises_and_does_not_revoke(user, httpx_mock):
    conn = await _oauth_conn(user, "commcare_connect")
    t = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="9", canonical_name="Keep"
    )
    await TenantMembership.all_objects.acreate(user=user, tenant=t, connection=conn)

    httpx_mock.add_response(url=CONNECT_URL, json={"unexpected_key": []})  # drift, not zero-tenants
    with pytest.raises(TenantResolutionError):
        await resolve_connect_opportunities(user, "tok")

    assert await TenantMembership.objects.filter(user=user, tenant=t).aexists()  # untouched


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_commcare_pagination_follows_relative_next(user, httpx_mock):
    httpx_mock.add_response(
        url=COMMCARE_DOMAIN_API,
        json={
            "objects": [{"domain_name": "a", "project_name": "A"}],
            "meta": {"next": "/api/user_domains/v1/?offset=1"},
        },
    )
    httpx_mock.add_response(
        url="https://www.commcarehq.org/api/user_domains/v1/?offset=1",
        json={"objects": [{"domain_name": "b", "project_name": "B"}], "meta": {"next": None}},
    )
    domains = await _fetch_all_domains("tok")
    assert {d["domain_name"] for d in domains} == {"a", "b"}  # page 2 not silently dropped


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_ocs_archival_is_scoped_to_token_team(user):
    # user's token is scoped to team-a
    await SocialAccount.objects.acreate(
        user=user, provider="ocs", uid="u1", extra_data={"team": "team-a"}
    )
    conn = await _oauth_conn(user, "ocs")
    t_a_stale = await Tenant.objects.acreate(provider="ocs", external_id="A1", canonical_name="A1")
    t_b = await Tenant.objects.acreate(provider="ocs", external_id="B1", canonical_name="B1")
    # a stale team-a chatbot (should be revoked) and a team-b chatbot (must be left alone)
    tm_a = await TenantMembership.all_objects.acreate(user=user, tenant=t_a_stale, connection=conn)
    tm_a.team_slug = "team-a"
    await tm_a.asave(update_fields=["provider_metadata"])
    tm_b = await TenantMembership.all_objects.acreate(user=user, tenant=t_b, connection=conn)
    tm_b.team_slug = "team-b"
    await tm_b.asave(update_fields=["provider_metadata"])

    async def fake_get(*args, **kwargs):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "results": [{"id": "A2", "name": "A2"}],
                    "next": None,
                }  # only a new team-a bot

        return R()

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value.get = AsyncMock(side_effect=fake_get)
        await resolve_ocs_chatbots(user, "tok")

    # team-a stale membership revoked; team-b membership untouched; new team-a bot added
    assert not await TenantMembership.objects.filter(user=user, tenant=t_a_stale).aexists()
    assert await TenantMembership.objects.filter(user=user, tenant=t_b).aexists()  # other team safe
    assert await TenantMembership.objects.filter(user=user, tenant__external_id="A2").aexists()
