from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.db import connection as django_connection
from django.db import transaction
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.adapters import encrypt_credential
from apps.users.models import (
    Tenant,
    TenantConnection,
    TenantMembership,
    UpstreamAccessProof,
    VerificationControl,
)
from apps.users.services.access_verification import (
    VerificationAttemptReceipt,
    claim_verification,
    publish_verification,
)
from apps.users.services.access_verification_service import (
    _durable_result,
    verify_connection_access,
)
from apps.users.services.access_verification_types import (
    AccessVerificationStatus,
    ProviderVerificationResult,
    VerificationOutcome,
    VerificationResult,
)
from apps.users.services.token_refresh import credential_fingerprint


@pytest.fixture
def api_connection(user, tenant):
    connection = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("username:key"),
    )
    membership = TenantMembership.objects.create(user=user, tenant=tenant, connection=connection)
    return connection, membership


def _hold_user_lock(user_id, acquired, release):
    try:
        with transaction.atomic():
            user_model = TenantConnection._meta.get_field("user").remote_field.model
            user_model.objects.select_for_update().get(pk=user_id)
            acquired.set()
            assert release.wait(timeout=10)
    finally:
        django_connection.close()


def _hold_control_lock(connection_id, acquired, release):
    try:
        with transaction.atomic():
            VerificationControl.objects.select_for_update().get(connection_id=connection_id)
            acquired.set()
            assert release.wait(timeout=10)
    finally:
        django_connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["deadline", "cancellation"])
async def test_oauth_refresh_holds_network_slot_until_cancelled_request_stops(monkeypatch, finish):
    from apps.users.services import access_verification_service

    claim = SimpleNamespace(
        request=SimpleNamespace(token_snapshot=(1, "refresh", 1)),
        observation=SimpleNamespace(provider="commcare"),
    )
    token = SimpleNamespace(
        token_secret="refresh",
        app=object(),
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    refresh_started = asyncio.Event()
    refresh_stopped = asyncio.Event()
    waiter_acquired_before_stop = False
    limiter = asyncio.Semaphore(1)

    async def load_token(*args, **kwargs):
        return token

    async def blocking_refresh(*args, **kwargs):
        refresh_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            refresh_stopped.set()

    async def wait_for_network_slot():
        nonlocal waiter_acquired_before_stop
        await limiter.acquire()
        waiter_acquired_before_stop = not refresh_stopped.is_set()
        limiter.release()

    monkeypatch.setattr(access_verification_service, "_load_claim_token", load_token)
    monkeypatch.setattr(access_verification_service, "refresh_oauth_token_result", blocking_refresh)
    deadline = time.monotonic() + (0.1 if finish == "deadline" else 5)
    operation = asyncio.create_task(
        access_verification_service._refresh_claim_if_needed(
            claim,
            deadline=deadline,
            clock=time.monotonic,
            limiter=limiter,
        )
    )
    await asyncio.wait_for(refresh_started.wait(), timeout=1)
    waiter = asyncio.create_task(wait_for_network_slot())
    if finish == "cancellation":
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        _claim, result = await operation
        assert result.outcome == VerificationOutcome.UNAVAILABLE
    await asyncio.wait_for(waiter, timeout=1)

    assert refresh_stopped.is_set()
    assert waiter_acquired_before_stop is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_fresh_proof_returns_without_provider_network(user, tenant, api_connection):
    connection, _membership = api_connection
    now = timezone.now()
    claim = await sync_to_async(claim_verification)(user.id, connection.id, {tenant.id}, now=now)
    await sync_to_async(publish_verification)(
        claim, VerificationResult.complete({tenant.id}), now=now
    )
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ProviderVerificationResult.complete({tenant.external_id})

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert calls == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_disjoint_concurrent_requests_share_one_provider_call(user, tenant, api_connection):
    connection, _membership = api_connection
    other = await Tenant.objects.acreate(
        provider="commcare", external_id="other-domain", canonical_name="Other"
    )
    await TenantMembership.objects.acreate(user=user, tenant=other, connection=connection)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProviderVerificationResult.complete({tenant.external_id, other.external_id})

    first = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    second = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {other.id}, provider_verifier=provider)
    )
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(first, second)

    assert {result.status for result in results} == {AccessVerificationStatus.VERIFIED}
    assert calls == 1
    assert await UpstreamAccessProof.objects.filter(connection=connection).acount() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_complete_recovery_maps_only_observed_connection_history(
    user, tenant, api_connection
):
    connection, membership = api_connection
    await TenantMembership.all_objects.filter(pk=membership.pk).aupdate(archived_at=timezone.now())
    other_tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="other-owned", canonical_name="Other"
    )
    other_connection = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("other:key"),
    )
    other_membership = await TenantMembership.all_objects.acreate(
        user=user,
        tenant=other_tenant,
        connection=other_connection,
        archived_at=timezone.now(),
    )

    async def provider(*args, **kwargs):
        return ProviderVerificationResult.complete(
            {tenant.external_id, other_tenant.external_id, "unknown-upstream-id"}
        )

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert await TenantMembership.objects.filter(pk=membership.pk).aexists()
    assert not await TenantMembership.objects.filter(pk=other_membership.pk).aexists()
    assert not await Tenant.objects.filter(external_id="unknown-upstream-id").aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_complete_ocs_listing_does_not_verify_requested_other_team_history(user):
    account = await SocialAccount.objects.acreate(
        user=user,
        provider="ocs",
        uid="identity#acme",
        extra_data={"team": "acme"},
    )
    await SocialToken.objects.acreate(account=account, token="access")
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
        scope_key="acme",
    )
    other_team = await Tenant.objects.acreate(
        provider="ocs", external_id="other-team-bot", canonical_name="Other team"
    )
    membership = await TenantMembership.objects.acreate(
        user=user,
        tenant=other_team,
        connection=connection,
        provider_metadata={"team_slug": "globex"},
    )

    async def provider(*args, **kwargs):
        return ProviderVerificationResult.complete({other_team.external_id})

    result = await verify_connection_access(
        user.id, connection.id, {other_team.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.DENIED
    assert await TenantMembership.objects.filter(pk=membership.pk).aexists()
    assert not await UpstreamAccessProof.objects.filter(
        connection=connection, tenant=other_team
    ).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_complete_listing_does_not_verify_membership_rebound_before_publication(
    user, tenant, api_connection, monkeypatch
):
    from apps.users.services import access_verification_service

    connection, membership = api_connection
    replacement = await TenantConnection.objects.acreate(
        user=user,
        provider=tenant.provider,
        credential_type=TenantConnection.API_KEY,
        encrypted_credential=encrypt_credential("replacement:key"),
    )
    original = access_verification_service.apublish_verification_receipt

    async def rebind_then_publish(*args, **kwargs):
        await TenantMembership.all_objects.filter(pk=membership.pk).aupdate(connection=replacement)
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        access_verification_service, "apublish_verification_receipt", rebind_then_publish
    )

    async def provider(*args, **kwargs):
        return ProviderVerificationResult.complete({tenant.external_id})

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.DENIED
    assert not await UpstreamAccessProof.objects.filter(
        connection=connection, tenant=tenant
    ).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_oauth_refresh_rebases_claim_to_persisted_token_before_provider(
    user, tenant, httpx_mock
):
    app = await SocialApp.objects.acreate(
        provider="commcare", name="CommCare", client_id="client", secret="secret"
    )
    account = await SocialAccount.objects.acreate(user=user, provider="commcare", uid="identity")
    token = await SocialToken.objects.acreate(
        account=account,
        app=app,
        token="old-access",
        token_secret="old-refresh",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    await TenantMembership.objects.acreate(user=user, tenant=tenant, connection=connection)
    httpx_mock.add_response(
        url="https://www.commcarehq.org/oauth/token/",
        method="POST",
        json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900},
    )

    async def provider(snapshot, **kwargs):
        assert snapshot.credential == "new-access"
        return ProviderVerificationResult.complete({tenant.external_id})

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    await token.arefresh_from_db()
    proof = await UpstreamAccessProof.objects.aget(connection=connection, tenant=tenant)
    assert proof.credential_fingerprint == credential_fingerprint(token)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_outcome", "rotate_before_waiter", "expected_calls"),
    [
        # A COMPLETE receipt excludes the tenant it omitted, so this waiter
        # re-verifies; a rejected credential is connection-wide and is reused.
        ("complete", False, 2),
        ("credential_rejected", False, 1),
        ("complete", True, 2),
    ],
)
async def test_pre_refresh_waiter_consumes_rebased_attempt_receipt(
    user,
    tenant,
    httpx_mock,
    monkeypatch,
    provider_outcome,
    rotate_before_waiter,
    expected_calls,
):
    from apps.users.services import access_verification_service

    app = await SocialApp.objects.acreate(
        provider="commcare", name="CommCare", client_id="client", secret="secret"
    )
    account = await SocialAccount.objects.acreate(user=user, provider="commcare", uid="identity")
    await SocialToken.objects.acreate(
        account=account,
        app=app,
        token="old-access",
        token_secret="old-refresh",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    await TenantMembership.objects.acreate(user=user, tenant=tenant, connection=connection)
    omitted = await Tenant.objects.acreate(
        provider="commcare", external_id="oauth-omitted", canonical_name="OAuth Omitted"
    )
    await TenantMembership.objects.acreate(user=user, tenant=omitted, connection=connection)
    httpx_mock.add_response(
        url="https://www.commcarehq.org/oauth/token/",
        method="POST",
        json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900},
    )
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    original_refresh = access_verification_service.refresh_oauth_token_result

    async def paused_refresh(*args, **kwargs):
        refresh_started.set()
        await release_refresh.wait()
        return await original_refresh(*args, **kwargs)

    monkeypatch.setattr(access_verification_service, "refresh_oauth_token_result", paused_refresh)
    calls = 0
    waiter_polling = asyncio.Event()
    release_waiter = asyncio.Event()

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if provider_outcome == "complete":
            return ProviderVerificationResult.complete({tenant.external_id})
        return ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED)

    async def controlled_waiter_sleep(_delay):
        waiter_polling.set()
        await release_waiter.wait()

    winner = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(refresh_started.wait(), timeout=2)
    waiter = asyncio.create_task(
        verify_connection_access(
            user.id,
            connection.id,
            {omitted.id},
            provider_verifier=provider,
            sleep=controlled_waiter_sleep,
        )
    )
    await asyncio.wait_for(waiter_polling.wait(), timeout=2)
    release_refresh.set()
    winner_result = await winner
    if rotate_before_waiter:
        await SocialToken.objects.filter(account=account).aupdate(token="unrelated-rotation")
    release_waiter.set()
    waiter_result = await waiter

    expected_winner = (
        AccessVerificationStatus.VERIFIED
        if provider_outcome == "complete"
        else AccessVerificationStatus.DENIED
    )
    expected_waiter_code = (
        "upstream_access_lost" if provider_outcome == "complete" else ErrorCode.AUTH_TOKEN_EXPIRED
    )
    assert winner_result.status == expected_winner
    assert waiter_result.status == AccessVerificationStatus.DENIED
    assert waiter_result.error_code == expected_waiter_code
    assert calls == expected_calls


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rotation_during_provider_call_cannot_publish_stale_success(
    user, tenant, api_connection
):
    connection, _membership = api_connection

    async def provider(*args, **kwargs):
        await TenantConnection.objects.filter(pk=connection.pk).aupdate(
            encrypted_credential=encrypt_credential("replacement:key")
        )
        return ProviderVerificationResult.complete({tenant.external_id})

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.RETRY
    assert not await UpstreamAccessProof.objects.filter(connection=connection).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_denial_during_refresh_rebase_releases_lease(user, tenant, httpx_mock, monkeypatch):
    from apps.users.services import access_verification_service

    app = await SocialApp.objects.acreate(
        provider="commcare", name="CommCare", client_id="client", secret="secret"
    )
    account = await SocialAccount.objects.acreate(user=user, provider="commcare", uid="identity")
    await SocialToken.objects.acreate(
        account=account,
        app=app,
        token="old-access",
        token_secret="old-refresh",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    await TenantMembership.objects.acreate(user=user, tenant=tenant, connection=connection)
    httpx_mock.add_response(
        url="https://www.commcarehq.org/oauth/token/",
        method="POST",
        json={"access_token": "new-access", "refresh_token": "new-refresh"},
    )
    original = access_verification_service.refresh_oauth_token_result

    async def deny_after_refresh(*args, **kwargs):
        result = await original(*args, **kwargs)
        await TenantConnection.objects.filter(pk=connection.pk).aupdate(
            upstream_denied_at=timezone.now()
        )
        return result

    monkeypatch.setattr(
        access_verification_service, "refresh_oauth_token_result", deny_after_refresh
    )
    provider_called = False

    async def provider(*args, **kwargs):
        nonlocal provider_called
        provider_called = True
        return ProviderVerificationResult.complete({tenant.external_id})

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=provider
    )

    assert result.status == AccessVerificationStatus.RETRY
    assert provider_called is False
    control = await VerificationControl.objects.aget(connection=connection)
    assert control.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_token_rotation_before_refresh_load_releases_lease(user, tenant, monkeypatch):
    from apps.users.services import access_verification_service

    app = await SocialApp.objects.acreate(
        provider="commcare", name="CommCare", client_id="client", secret="secret"
    )
    account = await SocialAccount.objects.acreate(user=user, provider="commcare", uid="identity")
    await SocialToken.objects.acreate(
        account=account,
        app=app,
        token="old-access",
        token_secret="old-refresh",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    await TenantMembership.objects.acreate(user=user, tenant=tenant, connection=connection)

    async def missing_token(*args, **kwargs):
        return None

    monkeypatch.setattr(access_verification_service, "_load_claim_token", missing_token)

    result = await verify_connection_access(user.id, connection.id, {tenant.id})

    assert result.status == AccessVerificationStatus.RETRY
    control = await VerificationControl.objects.aget(connection=connection)
    assert control.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_waiter_converges_on_durable_unavailable_result_without_second_call(
    user, tenant, api_connection
):
    connection, _membership = api_connection
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProviderVerificationResult.unavailable("provider_timeout")

    first = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    second = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(first, second)

    assert {result.status for result in results} == {AccessVerificationStatus.UNAVAILABLE}
    assert calls == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_result", "expected_status"),
    [
        (
            ProviderVerificationResult.unavailable("provider_timeout"),
            AccessVerificationStatus.UNAVAILABLE,
        ),
        (
            ProviderVerificationResult.indeterminate("invalid_shape"),
            AccessVerificationStatus.INDETERMINATE,
        ),
    ],
)
async def test_disjoint_waiter_observes_connection_attempt_failure(
    user, tenant, api_connection, provider_result, expected_status
):
    connection, _membership = api_connection
    other = await Tenant.objects.acreate(
        provider="commcare", external_id="other-waiter", canonical_name="Other"
    )
    await TenantMembership.objects.acreate(user=user, tenant=other, connection=connection)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return provider_result

    winner = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    waiter = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {other.id}, provider_verifier=provider)
    )
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(winner, waiter)

    assert [result.status for result in results] == [expected_status, expected_status]
    assert calls == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_omitted_waiter_reverifies_rather_than_reading_a_receipt_that_skipped_it(
    user, tenant, api_connection
):
    """A COMPLETE receipt is authoritative only for the tenants it confirmed live.

    The waiter's tenant was omitted from that publication, so the receipt carries no
    verdict for it and the waiter must ask upstream itself. It still ends denied --
    but on its own evidence, not by inference from an attempt that never covered it.
    """
    connection, _membership = api_connection
    omitted = await Tenant.objects.acreate(
        provider="commcare", external_id="omitted-waiter", canonical_name="Omitted"
    )
    await TenantMembership.objects.acreate(user=user, tenant=omitted, connection=connection)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProviderVerificationResult.complete({tenant.external_id})

    winner = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    waiter = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {omitted.id}, provider_verifier=provider)
    )
    await asyncio.sleep(0.05)
    release.set()
    winner_result, waiter_result = await asyncio.gather(winner, waiter)

    assert winner_result.status == AccessVerificationStatus.VERIFIED
    assert waiter_result.status == AccessVerificationStatus.DENIED
    assert waiter_result.error_code == "upstream_access_lost"
    # Two discoveries: the omitted waiter cannot reuse a scope it falls outside.
    assert calls == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_disjoint_waiter_observes_credential_rejection_without_second_discovery(
    user, tenant, api_connection
):
    connection, _membership = api_connection
    other = await Tenant.objects.acreate(
        provider="commcare", external_id="rejected-waiter", canonical_name="Rejected"
    )
    await TenantMembership.objects.acreate(user=user, tenant=other, connection=connection)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED)

    winner = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    waiter = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {other.id}, provider_verifier=provider)
    )
    await asyncio.sleep(0.05)
    release.set()
    winner_result, waiter_result = await asyncio.gather(winner, waiter)

    assert winner_result.status == AccessVerificationStatus.DENIED
    assert waiter_result.status == AccessVerificationStatus.DENIED
    assert winner_result.error_code == ErrorCode.AUTH_TOKEN_EXPIRED
    assert waiter_result.error_code == ErrorCode.AUTH_TOKEN_EXPIRED
    assert calls == 1

    async def recovery_provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ProviderVerificationResult.complete({tenant.external_id, other.external_id})

    recovery = await verify_connection_access(
        user.id,
        connection.id,
        {other.id},
        provider_verifier=recovery_provider,
    )

    assert recovery.status == AccessVerificationStatus.VERIFIED
    assert calls == 2
    recovered = await TenantMembership.all_objects.aget(
        user=user, tenant=other, connection=connection
    )
    assert recovered.archived_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_cancellation_releases_only_owned_lease(user, tenant, api_connection):
    connection, _membership = api_connection
    started = asyncio.Event()

    async def provider(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    control = await VerificationControl.objects.aget(connection=connection)
    assert control.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_cancellation_during_initial_claim_drains_and_releases_lease(
    user, tenant, api_connection
):
    connection, _membership = api_connection
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_user_lock, args=(user.id, acquired, release))
    locker.start()
    assert await asyncio.to_thread(acquired.wait, 2)

    task = asyncio.create_task(verify_connection_access(user.id, connection.id, {tenant.id}))
    await asyncio.sleep(0.05)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(locker.join, 2)
    await asyncio.sleep(0.05)

    control = await VerificationControl.objects.filter(connection=connection).afirst()
    assert control is None or control.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_cancellation_during_waiter_takeover_does_not_create_late_claim(
    user, tenant, api_connection, monkeypatch
):
    from apps.users.services import access_verification_service

    connection, _membership = api_connection
    await VerificationControl.objects.acreate(
        connection=connection,
        lease_token="54e4fd04-a81c-46cf-9aae-fc627c206b89",
        lease_expires_at=timezone.now() + timedelta(seconds=1),
    )
    original = access_verification_service.aclaim_verification
    takeover_started = asyncio.Event()
    finish_takeover = asyncio.Event()
    calls = 0

    async def controlled_claim(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original(*args, **kwargs)
        takeover_started.set()
        try:
            await finish_takeover.wait()
        except asyncio.CancelledError:
            await finish_takeover.wait()
        return await original(*args, now=timezone.now() + timedelta(seconds=5), **kwargs)

    monkeypatch.setattr(access_verification_service, "aclaim_verification", controlled_claim)
    provider_called = False

    async def provider(*args, **kwargs):
        nonlocal provider_called
        provider_called = True
        return ProviderVerificationResult.complete({tenant.external_id})

    task = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(takeover_started.wait(), timeout=2)
    task.cancel()
    finish_takeover.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    control = await VerificationControl.objects.aget(connection=connection)
    assert str(control.lease_token) == "54e4fd04-a81c-46cf-9aae-fc627c206b89"
    assert provider_called is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_queued_claim_crossing_deadline_cannot_leave_late_lease(user, tenant, api_connection):
    connection, _membership = api_connection
    executor_started = threading.Event()
    executor_release = threading.Event()

    def occupy_thread_sensitive_executor():
        executor_started.set()
        assert executor_release.wait(timeout=10)

    blocker = asyncio.create_task(sync_to_async(occupy_thread_sensitive_executor)())
    assert await asyncio.to_thread(executor_started.wait, 2)
    started = time.monotonic()
    result = await verify_connection_access(
        user.id,
        connection.id,
        {tenant.id},
        deadline=started + 0.1,
    )
    elapsed = time.monotonic() - started
    executor_release.set()
    await blocker
    await asyncio.sleep(0.05)

    assert result.status == AccessVerificationStatus.UNAVAILABLE
    assert elapsed < 0.25
    assert not await VerificationControl.objects.filter(connection=connection).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_queued_result_mapping_cannot_publish_after_deadline(user, tenant, api_connection):
    connection, _membership = api_connection
    executor_started = threading.Event()
    executor_release = threading.Event()
    blocker = None

    def occupy_thread_sensitive_executor():
        executor_started.set()
        assert executor_release.wait(timeout=10)

    async def provider(*args, **kwargs):
        nonlocal blocker
        blocker = asyncio.create_task(sync_to_async(occupy_thread_sensitive_executor)())
        assert await asyncio.to_thread(executor_started.wait, 2)
        return ProviderVerificationResult.complete({tenant.external_id})

    timer = threading.Timer(0.5, executor_release.set)
    timer.start()
    started = time.monotonic()
    result = await verify_connection_access(
        user.id,
        connection.id,
        {tenant.id},
        deadline=started + 0.1,
        provider_verifier=provider,
    )
    elapsed = time.monotonic() - started
    blocker_still_held_at_return = not executor_release.is_set()
    executor_release.set()
    if blocker is not None:
        await blocker
    await asyncio.sleep(0.1)
    timer.cancel()

    assert result.status == AccessVerificationStatus.UNAVAILABLE
    assert elapsed < 0.3
    assert blocker_still_held_at_return
    assert not await UpstreamAccessProof.objects.filter(
        connection=connection, tenant=tenant
    ).aexists()
    control = await VerificationControl.objects.aget(connection=connection)
    assert control.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_cancellation_cleanup_is_bounded_when_control_is_locked(user, tenant, api_connection):
    connection, _membership = api_connection
    provider_started = asyncio.Event()

    async def provider(*args, **kwargs):
        provider_started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(provider_started.wait(), timeout=2)
    control = await VerificationControl.objects.aget(connection=connection)
    owned_lease = control.lease_token
    acquired = threading.Event()
    release = threading.Event()
    locker = threading.Thread(target=_hold_control_lock, args=(connection.id, acquired, release))
    locker.start()
    assert await asyncio.to_thread(acquired.wait, 2)
    timer = threading.Timer(0.5, release.set)
    timer.start()

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    blocker_still_held_at_return = not release.is_set()
    release.set()
    await asyncio.to_thread(locker.join, 2)
    timer.cancel()

    assert elapsed < 0.2
    assert blocker_still_held_at_return
    control = await VerificationControl.objects.aget(connection=connection)
    assert control.lease_token == owned_lease


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_publication_lock_wait_crossing_deadline_cannot_publish_success(
    user, tenant, api_connection
):
    connection, _membership = api_connection
    acquired = threading.Event()
    release = threading.Event()
    locker = None

    async def provider(*args, **kwargs):
        nonlocal locker
        locker = threading.Thread(target=_hold_user_lock, args=(user.id, acquired, release))
        locker.start()
        assert await asyncio.to_thread(acquired.wait, 2)
        return ProviderVerificationResult.complete({tenant.external_id})

    timer = threading.Timer(0.5, release.set)
    timer.start()
    started = time.monotonic()
    result = await verify_connection_access(
        user.id,
        connection.id,
        {tenant.id},
        deadline=time.monotonic() + 0.15,
        provider_verifier=provider,
    )
    elapsed = time.monotonic() - started
    blocker_still_held_at_return = not release.is_set()
    release.set()
    if locker is not None:
        await asyncio.to_thread(locker.join, 2)
    timer.cancel()

    assert result.status == AccessVerificationStatus.UNAVAILABLE
    assert elapsed < 0.3
    assert blocker_still_held_at_return
    assert not await UpstreamAccessProof.objects.filter(
        connection=connection, tenant=tenant
    ).aexists()
    control = await VerificationControl.objects.aget(connection=connection)
    assert control.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_waiter_takes_over_expired_lease(user, tenant, api_connection):
    connection, _membership = api_connection
    await VerificationControl.objects.acreate(
        connection=connection,
        lease_token="54e4fd04-a81c-46cf-9aae-fc627c206b89",
        lease_expires_at=timezone.now() + timedelta(milliseconds=100),
    )
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ProviderVerificationResult.complete({tenant.external_id})

    result = await verify_connection_access(
        user.id,
        connection.id,
        {tenant.id},
        deadline=time.monotonic() + 2,
        provider_verifier=provider,
    )

    assert result.status == AccessVerificationStatus.VERIFIED
    assert calls == 1


@pytest.mark.django_db
def test_publication_uses_provider_completion_time_for_freshness(user, tenant, api_connection):
    connection, _membership = api_connection
    started = timezone.now()
    claim = claim_verification(user.id, connection.id, {tenant.id}, now=started)
    completion = started + timedelta(seconds=4)
    published = started + timedelta(seconds=20)

    publish_verification(
        claim,
        VerificationResult.complete({tenant.id}),
        now=published,
        verified_at=completion,
    )

    proof = UpstreamAccessProof.objects.get(connection=connection, tenant=tenant)
    assert proof.verified_at == completion


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_covered_waiter_reads_a_complete_receipt_as_verified(user, tenant, api_connection):
    """A waiter inside the receipt's confirmed scope was verified, not denied.

    Before the receipt carried a tenant scope, a matching COMPLETE could only mean
    "the winner finished and I was not in it", so the waiter was denied. Now the
    scope names the tenants the attempt confirmed live, and this waiter is one of
    them -- denying it would revoke access the same attempt had just proven.
    """
    connection, _membership = api_connection
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProviderVerificationResult.complete({tenant.external_id})

    winner = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    waiter = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.sleep(0.05)
    release.set()
    winner_result, waiter_result = await asyncio.gather(winner, waiter)

    assert winner_result.status == AccessVerificationStatus.VERIFIED
    assert waiter_result.status == AccessVerificationStatus.VERIFIED
    # Reused the winner's proof instead of asking upstream a second time.
    assert calls == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_denied_waiter_reads_a_tenant_denied_receipt_as_denied(user, tenant, api_connection):
    """TENANT_DENIED records the denied tenants, so a covered waiter is denied.

    This is the one outcome that can hand a waiter a definitive denial, and the
    durable path previously ignored it and fell through to a second discovery.
    """
    connection, _membership = api_connection
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ProviderVerificationResult.tenant_denied(
            tenant.external_id, ErrorCode.AUTH_ACCESS_DENIED
        )

    winner = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    waiter = asyncio.create_task(
        verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)
    )
    await asyncio.sleep(0.05)
    release.set()
    winner_result, waiter_result = await asyncio.gather(winner, waiter)

    assert winner_result.status == AccessVerificationStatus.DENIED
    assert waiter_result.status == AccessVerificationStatus.DENIED
    assert waiter_result.error_code == ErrorCode.AUTH_ACCESS_DENIED
    assert calls == 1


def test_durable_result_fails_closed_when_the_receipt_skipped_the_caller():
    """The grant branch re-checks coverage, so a caller that skipped the match is denied.

    attempt_receipt_matches already enforces this subset, so the normal path cannot
    reach here -- but this is the only branch that grants access, and a future caller
    that forgets the match must fail closed rather than inherit someone else's proof.
    """
    covered = uuid.uuid4()
    uncovered = uuid.uuid4()
    receipt = VerificationAttemptReceipt(
        lease_token=uuid.uuid4(),
        outcome=VerificationOutcome.COMPLETE,
        error_code="",
        observation_hash="hash",
        tenant_ids=frozenset({str(covered)}),
    )

    assert _durable_result(receipt, {covered}).status == AccessVerificationStatus.VERIFIED

    denied = _durable_result(receipt, {covered, uncovered})
    assert denied.status == AccessVerificationStatus.DENIED
    assert denied.error_code == "upstream_access_lost"
