"""A provider check the caller stopped waiting for still finishes and publishes."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.utils import timezone

from apps.users.adapters import encrypt_credential
from apps.users.models import (
    TenantConnection,
    TenantMembership,
    UpstreamAccessProof,
    VerificationControl,
)
from apps.users.services import access_verification_service
from apps.users.services.access_verification_service import verify_connection_access
from apps.users.services.access_verification_types import (
    AccessVerificationStatus,
    ProviderVerificationResult,
)
from apps.workspaces.services import access_freshness
from apps.workspaces.services.access_freshness import (
    VERIFICATION_IN_PROGRESS,
    VerificationBudget,
    admit_upstream,
)
from tests.upstream_proofs import amake_proof_stale


@pytest.fixture
def api_connection(user, tenant):
    connection = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("username:key"),
    )
    TenantMembership.objects.create(user=user, tenant=tenant, connection=connection)
    return connection


async def _drain_supervised():
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(10):
        while pending := [
            task
            for task in access_verification_service._SUPERVISED_OPERATIONS
            if task.get_loop() is loop
        ]:
            await asyncio.wait(pending)


def _gated_provider(result):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def provider(*args, **kwargs):
        calls.append(kwargs)
        started.set()
        await release.wait()
        return result

    return provider, started, release, calls


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_slow_check_answers_in_progress_then_publishes_its_proof(
    user, tenant, api_connection
):
    provider, _started, release, _calls = _gated_provider(
        ProviderVerificationResult.complete({tenant.external_id})
    )
    now = time.monotonic()

    result = await verify_connection_access(
        user.id,
        api_connection.id,
        {tenant.id},
        deadline=now + 5,
        respond_by=now + 2,
        provider_verifier=provider,
    )

    assert result.status == AccessVerificationStatus.IN_PROGRESS
    assert not await UpstreamAccessProof.objects.filter(tenant=tenant).aexists()
    release.set()
    await _drain_supervised()
    proof = await UpstreamAccessProof.objects.aget(connection=api_connection, tenant=tenant)
    assert proof.verified_at is not None
    control = await VerificationControl.objects.aget(connection=api_connection)
    assert control.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_slow_check_that_finds_revocation_still_archives(user, tenant, api_connection):
    provider, _started, release, _calls = _gated_provider(
        ProviderVerificationResult.complete(set())
    )
    now = time.monotonic()

    result = await verify_connection_access(
        user.id,
        api_connection.id,
        {tenant.id},
        deadline=now + 5,
        respond_by=now + 2,
        provider_verifier=provider,
    )
    release.set()
    await _drain_supervised()

    assert result.status == AccessVerificationStatus.IN_PROGRESS
    assert not await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_concurrent_callers_coalesce_on_the_continuing_check(user, tenant, api_connection):
    provider, started, release, calls = _gated_provider(
        ProviderVerificationResult.complete({tenant.external_id})
    )
    now = time.monotonic()

    first = await verify_connection_access(
        user.id,
        api_connection.id,
        {tenant.id},
        deadline=now + 5,
        respond_by=now + 2,
        provider_verifier=provider,
    )
    await started.wait()
    second = await verify_connection_access(
        user.id,
        api_connection.id,
        {tenant.id},
        deadline=time.monotonic() + 5,
        respond_by=time.monotonic() + 2,
        provider_verifier=provider,
    )
    release.set()
    await _drain_supervised()
    third = await verify_connection_access(
        user.id, api_connection.id, {tenant.id}, provider_verifier=provider
    )

    assert first.status == AccessVerificationStatus.IN_PROGRESS
    # A waiter that runs out of time behind the lease never decides anything.
    assert second.status in {
        AccessVerificationStatus.IN_PROGRESS,
        AccessVerificationStatus.UNAVAILABLE,
    }
    assert third.status == AccessVerificationStatus.VERIFIED
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_without_respond_by_the_deadline_still_cancels(user, tenant, api_connection):
    provider, _started, _release, _calls = _gated_provider(
        ProviderVerificationResult.complete({tenant.external_id})
    )

    result = await verify_connection_access(
        user.id,
        api_connection.id,
        {tenant.id},
        deadline=time.monotonic() + 0.2,
        provider_verifier=provider,
    )
    await _drain_supervised()

    assert result.status == AccessVerificationStatus.UNAVAILABLE
    control = await VerificationControl.objects.aget(connection=api_connection)
    assert control.lease_token is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_sync_views_check_still_publishes_after_it_answers(
    user, workspace, tenant, upstream_provider, monkeypatch
):
    """DRF views are sync: they reach the check through async_to_sync, whose
    executor dies with the call. The continuing check must not depend on it."""
    monkeypatch.setitem(access_freshness.BUDGET_SECONDS, VerificationBudget.INTERACTIVE, 0.5)
    await amake_proof_stale(user, tenant)
    upstream_provider.domains = [tenant.external_id]
    upstream_provider.gate = asyncio.Event()

    # As Django's ASGI handler runs a sync view: in its own thread-sensitive context.
    async with ThreadSensitiveContext():
        admission = await sync_to_async(admit_upstream)(
            user.id, {tenant.id}, budget=VerificationBudget.INTERACTIVE
        )

    assert admission.reason == VERIFICATION_IN_PROGRESS
    upstream_provider.gate.set()
    await _drain_supervised()
    proof = await UpstreamAccessProof.objects.aget(connection__user=user, tenant=tenant)
    assert timezone.now() - proof.verified_at < timedelta(minutes=1)
