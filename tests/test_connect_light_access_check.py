"""A Connect recheck asks about the requested opportunities only, and decides only them."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from functools import partial

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.utils import timezone

from apps.users.models import (
    Tenant,
    TenantConnection,
    TenantMembership,
    UpstreamAccessProof,
    VerificationControl,
)
from apps.users.services.access_verification import (
    _completed_attempt,
    attempt_receipt_matches,
    claim_verification,
    publish_verification,
)
from apps.users.services.access_verification_providers import verify_provider
from apps.users.services.access_verification_service import verify_connection_access
from apps.users.services.access_verification_types import (
    AccessVerificationStatus,
    VerificationResult,
)


class _Client:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses[url]


def _json(status, payload, url):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", url))


OPP_7 = "https://connect.example/export/opportunity/7/"
OPP_8 = "https://connect.example/export/opportunity/8/"
LISTING = "https://connect.example/export/opp_org_program_list/"


@pytest.fixture
def connect_setup(db, user, settings):
    settings.CONNECT_API_URL = "https://connect.example"
    account = SocialAccount.objects.create(user=user, provider="commcare_connect", uid="identity")
    SocialToken.objects.create(account=account, token="access")
    connection = TenantConnection.objects.create(
        user=user,
        provider="commcare_connect",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    opp_7 = Tenant.objects.create(provider="commcare_connect", external_id="7", canonical_name="7")
    opp_8 = Tenant.objects.create(provider="commcare_connect", external_id="8", canonical_name="8")
    for tenant in (opp_7, opp_8):
        TenantMembership.objects.create(user=user, tenant=tenant, connection=connection)
    return connection, opp_7, opp_8


async def _verify(user, connection, tenant_ids, responses):
    client = _Client(responses)
    provider = partial(verify_provider, client_factory=lambda: client, limiter=asyncio.Semaphore(1))
    result = await verify_connection_access(
        user.id, connection.id, tenant_ids, provider_verifier=provider
    )
    return result, client.urls


async def _is_live(user, tenant):
    return await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_200_proves_the_requested_opportunity_and_leaves_siblings(
    user, connect_setup
):
    connection, opp_7, opp_8 = connect_setup

    result, urls = await _verify(
        user, connection, {opp_7.id}, {OPP_7: _json(200, {"id": 7}, OPP_7)}
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert urls == [OPP_7]
    assert await _is_live(user, opp_7)
    # The light check said nothing about 8; it must neither archive nor prove it.
    assert await _is_live(user, opp_8)
    assert await UpstreamAccessProof.objects.filter(connection=connection, tenant=opp_7).aexists()
    assert not await UpstreamAccessProof.objects.filter(
        connection=connection, tenant=opp_8
    ).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_no_access_404_revokes_only_that_opportunity(user, connect_setup):
    connection, opp_7, opp_8 = connect_setup

    result, urls = await _verify(
        user,
        connection,
        {opp_7.id},
        {OPP_7: _json(404, {"detail": "Not found."}, OPP_7)},
    )

    assert result.status == AccessVerificationStatus.DENIED
    assert urls == [OPP_7]
    assert not await _is_live(user, opp_7)
    assert await _is_live(user, opp_8)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_routing_404_revokes_nothing(user, connect_setup):
    connection, opp_7, opp_8 = connect_setup
    html_404 = httpx.Response(
        404,
        text="<!DOCTYPE html><html></html>",
        headers={"content-type": "text/html; charset=utf-8"},
        request=httpx.Request("GET", OPP_7),
    )
    listing_down = httpx.Response(503, request=httpx.Request("GET", LISTING))

    result, urls = await _verify(
        user, connection, {opp_7.id}, {OPP_7: html_404, LISTING: listing_down}
    )

    # Falls back to the listing, which is down: nothing decided, nothing revoked.
    assert urls == [OPP_7, LISTING]
    assert result.status == AccessVerificationStatus.UNAVAILABLE
    assert await _is_live(user, opp_7)
    assert await _is_live(user, opp_8)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_404_for_every_opportunity_archives_the_first_and_denies(user, connect_setup):
    connection, opp_7, opp_8 = connect_setup

    result, _urls = await _verify(
        user,
        connection,
        {opp_7.id, opp_8.id},
        {
            OPP_7: _json(404, {"detail": "Not found."}, OPP_7),
            OPP_8: _json(404, {"detail": "Not found."}, OPP_8),
        },
    )

    assert result.status == AccessVerificationStatus.DENIED
    assert not await _is_live(user, opp_7)
    assert not await _is_live(user, opp_8)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_checks_each_requested_opportunity(user, connect_setup):
    connection, opp_7, opp_8 = connect_setup

    result, urls = await _verify(
        user,
        connection,
        {opp_7.id, opp_8.id},
        {OPP_7: _json(200, {"id": 7}, OPP_7), OPP_8: _json(200, {"id": 8}, OPP_8)},
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert sorted(urls) == [OPP_7, OPP_8]
    assert LISTING not in urls


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_200_restores_only_its_own_archived_membership(user, connect_setup):
    connection, opp_7, opp_8 = connect_setup
    await TenantMembership.all_objects.filter(user=user).aupdate(archived_at=timezone.now())

    result, urls = await _verify(
        user, connection, {opp_7.id}, {OPP_7: _json(200, {"id": 7}, OPP_7)}
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert urls == [OPP_7]
    assert await _is_live(user, opp_7)
    assert not await _is_live(user, opp_8)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_denial_clears_the_denied_proof(user, connect_setup):
    connection, opp_7, _opp_8 = connect_setup
    await _verify(user, connection, {opp_7.id}, {OPP_7: _json(200, {"id": 7}, OPP_7)})
    # Let the proof go stale so the next call rechecks upstream.
    await UpstreamAccessProof.objects.filter(connection=connection).aupdate(
        verified_at=timezone.now() - timedelta(hours=1)
    )

    result, _urls = await _verify(
        user, connection, {opp_7.id}, {OPP_7: _json(404, {"detail": "Not found."}, OPP_7)}
    )

    assert result.status == AccessVerificationStatus.DENIED
    proof = await UpstreamAccessProof.objects.aget(connection=connection, tenant=opp_7)
    assert proof.verified_at is None
    # Archived as an omission: no connection-wide stamp staling the siblings' proofs.
    await connection.arefresh_from_db()
    assert connection.upstream_denied_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_one_denied_opportunity_denies_the_request(user, connect_setup):
    connection, opp_7, opp_8 = connect_setup

    result, _urls = await _verify(
        user,
        connection,
        {opp_7.id, opp_8.id},
        {
            OPP_7: _json(200, {"id": 7}, OPP_7),
            OPP_8: _json(404, {"detail": "Not found."}, OPP_8),
        },
    )

    assert result.status == AccessVerificationStatus.DENIED
    assert await _is_live(user, opp_7)
    assert not await _is_live(user, opp_8)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_many_opportunities_use_the_listing_and_archive_omissions(
    user, connect_setup
):
    connection, opp_7, opp_8 = connect_setup
    extra = []
    for external_id in ("101", "102", "103", "104", "105"):
        tenant = await Tenant.objects.acreate(
            provider="commcare_connect", external_id=external_id, canonical_name=external_id
        )
        await TenantMembership.objects.acreate(user=user, tenant=tenant, connection=connection)
        extra.append(tenant)
    listed = [{"id": int(t.external_id), "name": t.external_id} for t in [opp_7, *extra]]

    result, urls = await _verify(
        user,
        connection,
        {opp_7.id, *(t.id for t in extra)},
        {LISTING: _json(200, {"opportunities": listed}, LISTING)},
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert urls == [LISTING]
    # The full listing is authoritative for the whole connection, so 8 is archived.
    assert not await _is_live(user, opp_8)


@pytest.mark.django_db(transaction=True)
def test_scoped_receipt_does_not_cover_a_sibling_tenant(user, connect_setup):
    connection, opp_7, opp_8 = connect_setup
    claim = claim_verification(user.id, connection.id, {opp_7.id})

    publish_verification(claim, VerificationResult.complete({opp_7.id}, scope={opp_7.id}))

    control = VerificationControl.objects.get(connection=connection)
    receipt = _completed_attempt(control)
    assert attempt_receipt_matches(receipt, claim.lease_token, claim.observation, {opp_7.id})
    assert not attempt_receipt_matches(receipt, claim.lease_token, claim.observation, {opp_8.id})
    assert TenantMembership.objects.filter(user=user, tenant=opp_8).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_connect_no_access_404_is_recorded_even_if_a_later_check_would_fail(
    user, connect_setup
):
    connection, opp_7, opp_8 = connect_setup
    down = httpx.Response(503, request=httpx.Request("GET", OPP_8))

    result, urls = await _verify(
        user,
        connection,
        {opp_7.id, opp_8.id},
        {OPP_7: _json(404, {"detail": "Not found."}, OPP_7), OPP_8: down},
    )

    # The revocation is published; 8 had no answer, so it is left as it was.
    assert urls == [OPP_7, OPP_8]
    assert result.status == AccessVerificationStatus.DENIED
    assert not await _is_live(user, opp_7)
    assert await _is_live(user, opp_8)
