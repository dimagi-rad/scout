"""Proof renewal (#926): an active user's request never waits on the provider.

An interactive request admitted on a proof older than ``PROOF_RENEW_AFTER`` renews
it in the background, so the proof never reaches ``PROOF_MAX_AGE`` while the user
keeps working. Admission itself is unchanged: it still needs a proof younger than
``PROOF_MAX_AGE``, and renewal never admits anything older.
"""

import asyncio
from datetime import timedelta

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.utils import timezone

from apps.users.models import TenantMembership, UpstreamAccessProof
from apps.users.services import access_verification_service
from apps.users.services.access_verification import PROOF_MAX_AGE, PROOF_RENEW_AFTER
from apps.users.services.access_verification_service import verify_connection_access
from apps.users.services.access_verification_types import AccessVerificationStatus
from apps.workspaces.access import (
    TENANT_ACCESS_LOST,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.services.access_freshness import UPSTREAM_ACCESS_LOST, VerificationBudget

DUE = PROOF_RENEW_AFTER + timedelta(seconds=30)
NOT_DUE = PROOF_RENEW_AFTER - timedelta(seconds=30)


def _age_proof(user, tenant, age):
    UpstreamAccessProof.objects.filter(connection__user=user, tenant=tenant).update(
        verified_at=timezone.now() - age
    )


_aage_proof = sync_to_async(_age_proof)


async def _proof_age(user, tenant):
    proof = await UpstreamAccessProof.objects.aget(connection__user=user, tenant=tenant)
    return timezone.now() - proof.verified_at


async def _drain_background():
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(10):
        while pending := [
            task
            for task in access_verification_service._SUPERVISED_OPERATIONS
            if task.get_loop() is loop
        ]:
            await asyncio.wait(pending)


def test_renewal_lands_before_expiry():
    assert (
        timedelta(seconds=access_verification_service.PROVIDER_BUDGET_SECONDS)
        < PROOF_MAX_AGE - PROOF_RENEW_AFTER
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_due_proof_admits_at_once_and_renews_behind_the_request(
    user, workspace, tenant, upstream_provider
):
    await _aage_proof(user, tenant, DUE)
    upstream_provider.domains = [tenant.external_id]
    # A provider that never answers until released: the request must not wait on it.
    upstream_provider.gate = asyncio.Event()

    try:
        result = await asyncio.wait_for(aresolve_workspace_access_ex(user, workspace.id), 5)
        assert result.granted
        await asyncio.wait_for(upstream_provider.requested.wait(), timeout=10)
    finally:
        upstream_provider.gate.set()
    await _drain_background()

    assert len(upstream_provider.requests) == 1
    assert await _proof_age(user, tenant) < timedelta(minutes=1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_proof_not_yet_due_is_not_renewed(user, workspace, tenant, upstream_provider):
    await _aage_proof(user, tenant, NOT_DUE)

    assert (await aresolve_workspace_access_ex(user, workspace.id)).granted
    await _drain_background()

    assert upstream_provider.requests == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_concurrent_requests_share_one_renewal(user, workspace, tenant, upstream_provider):
    await _aage_proof(user, tenant, DUE)
    upstream_provider.domains = [tenant.external_id]
    upstream_provider.gate = asyncio.Event()

    try:
        results = await asyncio.gather(
            *(aresolve_workspace_access_ex(user, workspace.id) for _ in range(3))
        )
        await asyncio.wait_for(upstream_provider.requested.wait(), timeout=10)
    finally:
        upstream_provider.gate.set()
    await _drain_background()

    assert all(result.granted for result in results)
    assert len(upstream_provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_revocation_found_by_renewal_blocks_the_next_request(
    user, workspace, tenant, upstream_provider
):
    await _aage_proof(user, tenant, DUE)
    upstream_provider.domains = []

    assert (await aresolve_workspace_access_ex(user, workspace.id)).granted
    await _drain_background()

    assert not await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()
    denied = await aresolve_workspace_access_ex(user, workspace.id)
    assert denied.denied_reason in {TENANT_ACCESS_LOST, UPSTREAM_ACCESS_LOST}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_failed_renewal_leaves_the_proof_to_expire_as_before(
    user, workspace, tenant, upstream_provider
):
    await _aage_proof(user, tenant, DUE)
    upstream_provider.failure = 503

    assert (await aresolve_workspace_access_ex(user, workspace.id)).granted
    await _drain_background()

    assert len(upstream_provider.requests) == 1
    assert DUE <= await _proof_age(user, tenant) < PROOF_MAX_AGE
    assert await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_background_budget_does_not_renew(user, workspace, tenant, upstream_provider):
    await _aage_proof(user, tenant, DUE)

    result = await aresolve_workspace_access_ex(
        user, workspace.id, verification=VerificationBudget.BACKGROUND
    )
    await _drain_background()

    assert result.granted
    assert upstream_provider.requests == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_callers_under_the_server_loop_renew(user, workspace, tenant, upstream_provider):
    await _aage_proof(user, tenant, DUE)
    upstream_provider.domains = [tenant.external_id]

    # As Django's ASGI handler runs a sync view: in its own thread-sensitive context.
    async with ThreadSensitiveContext():
        result = await sync_to_async(resolve_workspace_access_ex)(user, workspace.id)
    await _drain_background()

    assert result.granted
    assert len(upstream_provider.requests) == 1
    assert await _proof_age(user, tenant) < timedelta(minutes=1)


@pytest.mark.django_db(transaction=True)
def test_no_renewal_without_a_loop_to_run_it_on(user, workspace, tenant, upstream_provider):
    """runserver, shell, management commands: a renewal could not outlive the call."""
    _age_proof(user, tenant, DUE)

    assert resolve_workspace_access_ex(user, workspace.id).granted
    assert upstream_provider.requests == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_renewal_max_age_never_extends_freshness(user, workspace, tenant, upstream_provider):
    """A ``fresh_max_age`` above the policy cannot make an expired proof count as fresh."""
    membership = await TenantMembership.objects.aget(user=user, tenant=tenant)
    await _aage_proof(user, tenant, PROOF_MAX_AGE + timedelta(minutes=1))
    upstream_provider.domains = [tenant.external_id]

    result = await verify_connection_access(
        user.id, membership.connection_id, {tenant.id}, fresh_max_age=timedelta(hours=1)
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert len(upstream_provider.requests) == 1
