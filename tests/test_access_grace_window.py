"""The grace window: an unreachable provider does not lock out a recently proven user.

The two properties every grace admission must keep:

1. The check keeps going, or re-runs, in the background.
2. If that check finds access revoked, the revocation is recorded and the very
   next request is denied, with no grace.
"""

import asyncio
import logging
from datetime import timedelta

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.models import (
    Tenant,
    TenantConnection,
    TenantMembership,
    UpstreamAccessProof,
    VerificationControl,
)
from apps.users.services import access_verification_service
from apps.users.services.access_verification import agrace_proof_tenant_ids
from apps.users.services.access_verification_service import (
    VERIFICATION_IN_PROGRESS_AFTER_REJECTION,
    VERIFICATION_UNAVAILABLE_AFTER_DENIAL,
    VERIFICATION_UNAVAILABLE_AFTER_REJECTION,
    verify_connection_access,
)
from apps.users.services.access_verification_types import (
    AccessVerificationResult,
    AccessVerificationStatus,
    ProviderVerificationResult,
)
from apps.users.services.upstream_denial import record_validated_upstream_denial
from apps.workspaces.access import (
    TENANT_ACCESS_LOST,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.services import access_freshness
from apps.workspaces.services.access_freshness import (
    CREDENTIAL_EXPIRED,
    UPSTREAM_ACCESS_LOST,
    VERIFICATION_INDETERMINATE,
    VERIFICATION_UNAVAILABLE,
    VerificationBudget,
)

GRACE_EVENT = "upstream_access_grace"


@pytest.fixture(autouse=True)
def grace_on(settings):
    settings.UPSTREAM_ACCESS_GRACE_SECONDS = 30 * 60


def _age_proof(user, tenant, age):
    UpstreamAccessProof.objects.filter(connection__user=user, tenant=tenant).update(
        verified_at=timezone.now() - age
    )


_aage_proof = sync_to_async(_age_proof)


async def _proof(user, tenant):
    return await UpstreamAccessProof.objects.aget(connection__user=user, tenant=tenant)


async def _drain_background():
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(10):
        while pending := [
            task
            for task in access_verification_service._SUPERVISED_OPERATIONS
            if task.get_loop() is loop
        ]:
            await asyncio.wait(pending)


def _fail_first_call(stub, status):
    """Only the request's own check fails; whatever comes after sees ``stub``'s state."""
    answer = stub.handle
    failed = []

    async def handle(request):
        if not failed:
            failed.append(request)
            stub.requests.append(request)
            return httpx.Response(status, request=request)
        return await answer(request)

    stub.handle = handle


def _grace_records(caplog):
    return [r for r in caplog.records if r.getMessage().startswith(GRACE_EVENT)]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_property_1_grace_admission_rechecks_in_background(
    user, workspace, tenant, upstream_provider, caplog
):
    caplog.set_level(logging.INFO, logger="apps.workspaces.services.access_freshness")
    await _aage_proof(user, tenant, timedelta(minutes=10))
    _fail_first_call(upstream_provider, 503)
    # The provider recovers; the background recheck must find it and publish.
    upstream_provider.domains = [tenant.external_id]

    result = await aresolve_workspace_access_ex(user, workspace.id)

    assert result.granted
    [record] = _grace_records(caplog)
    assert record.levelno == logging.INFO
    await _drain_background()
    assert len(upstream_provider.requests) == 2
    proof = await _proof(user, tenant)
    assert timezone.now() - proof.verified_at < timedelta(minutes=1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_property_1_timed_out_check_keeps_going_and_publishes(
    user, workspace, tenant, upstream_provider, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="apps.workspaces.services.access_freshness")
    monkeypatch.setitem(access_freshness.BUDGET_SECONDS, VerificationBudget.INTERACTIVE, 0.5)
    await _aage_proof(user, tenant, timedelta(minutes=10))
    upstream_provider.domains = [tenant.external_id]
    upstream_provider.gate = asyncio.Event()

    result = await aresolve_workspace_access_ex(user, workspace.id)

    assert result.granted
    assert len(_grace_records(caplog)) == 1
    upstream_provider.gate.set()
    await _drain_background()
    # One provider call: the timed-out check finished rather than being redone.
    assert len(upstream_provider.requests) == 1
    proof = await _proof(user, tenant)
    assert timezone.now() - proof.verified_at < timedelta(minutes=1)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_property_2_background_revocation_blocks_the_next_request(
    user, workspace, tenant, upstream_provider, caplog
):
    caplog.set_level(logging.INFO, logger="apps.workspaces.services.access_freshness")
    await _aage_proof(user, tenant, timedelta(minutes=10))
    _fail_first_call(upstream_provider, 503)
    # The background recheck gets an authoritative answer: the tenant is gone.
    upstream_provider.domains = []

    assert (await aresolve_workspace_access_ex(user, workspace.id)).granted

    await _drain_background()
    assert not await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()
    # The provider is unreachable again, but the revocation is recorded: no grace.
    upstream_provider.failure = 503
    caplog.clear()

    denied = await aresolve_workspace_access_ex(user, workspace.id)

    assert not denied.granted
    assert denied.denied_reason in {TENANT_ACCESS_LOST, UPSTREAM_ACCESS_LOST}
    assert _grace_records(caplog) == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_property_2_timed_out_check_that_finds_revocation_blocks_next_request(
    user, workspace, tenant, upstream_provider, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="apps.workspaces.services.access_freshness")
    monkeypatch.setitem(access_freshness.BUDGET_SECONDS, VerificationBudget.INTERACTIVE, 0.5)
    await _aage_proof(user, tenant, timedelta(minutes=10))
    upstream_provider.domains = []
    upstream_provider.gate = asyncio.Event()

    assert (await aresolve_workspace_access_ex(user, workspace.id)).granted

    upstream_provider.gate.set()
    await _drain_background()
    upstream_provider.gate = None
    upstream_provider.failure = 503
    caplog.clear()

    denied = await aresolve_workspace_access_ex(user, workspace.id)

    assert not denied.granted
    assert _grace_records(caplog) == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_property_2_credential_rejected_in_background_blocks_next_request(
    user, workspace, tenant, upstream_provider, caplog
):
    caplog.set_level(logging.INFO, logger="apps.workspaces.services.access_freshness")
    await _aage_proof(user, tenant, timedelta(minutes=10))
    _fail_first_call(upstream_provider, 503)
    upstream_provider.failure = 401
    assert (await aresolve_workspace_access_ex(user, workspace.id)).granted

    await _drain_background()
    upstream_provider.failure = 503
    caplog.clear()

    denied = await aresolve_workspace_access_ex(user, workspace.id)

    assert not denied.granted
    assert denied.denied_reason in {TENANT_ACCESS_LOST, UPSTREAM_ACCESS_LOST}
    assert _grace_records(caplog) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (401, UPSTREAM_ACCESS_LOST),
        (403, VERIFICATION_INDETERMINATE),
    ],
)
def test_no_grace_for_an_authoritative_or_indeterminate_answer(
    user, workspace, tenant, upstream_provider, failure, reason, caplog
):
    caplog.set_level(logging.INFO, logger="apps.workspaces.services.access_freshness")
    _age_proof(user, tenant, timedelta(minutes=10))
    upstream_provider.failure = failure

    result = resolve_workspace_access_ex(user, workspace.id)

    assert not result.granted
    assert result.denied_reason in {reason, TENANT_ACCESS_LOST, CREDENTIAL_EXPIRED}
    assert _grace_records(caplog) == []


@pytest.mark.django_db(transaction=True)
def test_no_grace_when_the_provider_omits_the_tenant(user, workspace, tenant, upstream_provider):
    _age_proof(user, tenant, timedelta(minutes=10))
    upstream_provider.domains = []

    assert not resolve_workspace_access_ex(user, workspace.id).granted
    assert not TenantMembership.objects.filter(user=user, tenant=tenant).exists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_property_1_sync_callers_under_the_server_loop_recheck_in_background(
    user, workspace, tenant, upstream_provider
):
    """A sync (DRF) view runs in a thread of the server's loop, as under uvicorn."""
    await _aage_proof(user, tenant, timedelta(minutes=10))
    _fail_first_call(upstream_provider, 503)
    upstream_provider.domains = [tenant.external_id]

    # As Django's ASGI handler runs a sync view: in its own thread-sensitive context.
    async with ThreadSensitiveContext():
        result = await sync_to_async(resolve_workspace_access_ex)(user, workspace.id)

    assert result.granted
    await _drain_background()
    proof = await _proof(user, tenant)
    assert timezone.now() - proof.verified_at < timedelta(minutes=1)


@pytest.mark.django_db(transaction=True)
def test_no_grace_without_a_loop_to_recheck_on(user, workspace, tenant, upstream_provider):
    """runserver, shell, management commands: the recheck could not outlive the call."""
    _age_proof(user, tenant, timedelta(minutes=10))
    upstream_provider.failure = httpx.ConnectError("provider down")

    assert resolve_workspace_access_ex(user, workspace.id).denied_reason == (
        VERIFICATION_UNAVAILABLE
    )


@pytest.mark.django_db(transaction=True)
def test_no_grace_past_the_window(user, workspace, tenant, upstream_provider):
    _age_proof(user, tenant, timedelta(minutes=31))
    upstream_provider.failure = 503

    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.denied_reason == VERIFICATION_UNAVAILABLE


@pytest.mark.django_db(transaction=True)
def test_window_is_a_setting(settings, user, workspace, tenant, upstream_provider):
    settings.UPSTREAM_ACCESS_GRACE_SECONDS = 0
    _age_proof(user, tenant, timedelta(minutes=10))
    upstream_provider.failure = 503

    assert resolve_workspace_access_ex(user, workspace.id).denied_reason == (
        VERIFICATION_UNAVAILABLE
    )


@pytest.mark.django_db(transaction=True)
def test_no_grace_after_a_denial_recorded_since_the_proof(
    user, workspace, tenant, upstream_provider
):
    membership = TenantMembership.objects.get(user=user, tenant=tenant)
    other = Tenant.objects.create(provider="commcare", external_id="other", canonical_name="O")
    TenantMembership.objects.create(user=user, tenant=other, connection=membership.connection)
    _age_proof(user, tenant, timedelta(minutes=10))
    # A denial of a sibling tenant on the same connection moves its denial marker.
    record_validated_upstream_denial(
        membership.connection, code=ErrorCode.AUTH_ACCESS_DENIED, tenant_id=other.id
    )
    upstream_provider.failure = 503

    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.denied_reason == VERIFICATION_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_background_budget_gets_no_grace(user, workspace, tenant, upstream_provider):
    await _aage_proof(user, tenant, timedelta(minutes=10))
    upstream_provider.failure = 503

    result = await aresolve_workspace_access_ex(
        user, workspace.id, verification=VerificationBudget.BACKGROUND
    )

    assert result.denied_reason == VERIFICATION_UNAVAILABLE


@pytest.mark.parametrize(
    ("status", "code", "eligible"),
    [
        (AccessVerificationStatus.UNAVAILABLE, "verification_unavailable", True),
        (AccessVerificationStatus.IN_PROGRESS, "verification_in_progress", True),
        (AccessVerificationStatus.UNAVAILABLE, VERIFICATION_UNAVAILABLE_AFTER_REJECTION, False),
        (AccessVerificationStatus.IN_PROGRESS, VERIFICATION_IN_PROGRESS_AFTER_REJECTION, False),
        (AccessVerificationStatus.UNAVAILABLE, VERIFICATION_UNAVAILABLE_AFTER_DENIAL, False),
        (AccessVerificationStatus.IN_PROGRESS, "verification_in_progress_after_denial", False),
        (AccessVerificationStatus.UNAVAILABLE, ErrorCode.AUTH_TOKEN_EXPIRED, False),
        (AccessVerificationStatus.UNAVAILABLE, "verification_failed", False),
        (AccessVerificationStatus.INDETERMINATE, "verification_indeterminate", False),
        (AccessVerificationStatus.RETRY, "verification_retry", False),
        (AccessVerificationStatus.DENIED, ErrorCode.AUTH_ACCESS_DENIED, False),
    ],
)
def test_only_an_unanswered_check_is_grace_eligible(status, code, eligible):
    assert access_freshness._grace_eligible(AccessVerificationResult(status, code)) is eligible


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_unsettled_401_is_tagged_so_it_never_earns_grace(user, tenant):
    account = await SocialAccount.objects.acreate(user=user, provider="commcare_connect", uid="u")
    await SocialToken.objects.acreate(account=account, token="access")
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare_connect",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    opp = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="7", canonical_name="7"
    )
    await TenantMembership.objects.acreate(user=user, tenant=opp, connection=connection)
    answers = [
        ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED),
        ProviderVerificationResult.unavailable("verification_unavailable"),
    ]

    async def provider(*args, **kwargs):
        return answers.pop(0)

    async def no_sleep(_seconds):
        return None

    result = await verify_connection_access(
        user.id, connection.id, {opp.id}, provider_verifier=provider, sleep=no_sleep
    )

    assert answers == []
    assert result.status == AccessVerificationStatus.UNAVAILABLE
    assert result.error_code == VERIFICATION_UNAVAILABLE_AFTER_REJECTION
    assert not access_freshness._grace_eligible(result)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_unsettled_401_voids_the_proof_for_every_waiter(user, tenant):
    """Requests waiting on the same lease never see the leader's tag, so the 401
    must withdraw the proof grace would have relied on."""
    account = await SocialAccount.objects.acreate(user=user, provider="commcare_connect", uid="w")
    await SocialToken.objects.acreate(account=account, token="access")
    connection = await TenantConnection.objects.acreate(
        user=user,
        provider="commcare_connect",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    opp = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="8", canonical_name="8"
    )
    await TenantMembership.objects.acreate(user=user, tenant=opp, connection=connection)
    # Another workspace's tenant on the same connection, which a request waiting on
    # the same lease could be checking.
    sibling = await Tenant.objects.acreate(
        provider="commcare_connect", external_id="9", canonical_name="9"
    )
    await TenantMembership.objects.acreate(user=user, tenant=sibling, connection=connection)
    await UpstreamAccessProof.objects.acreate(
        connection=connection,
        tenant=sibling,
        credential_fingerprint="-",
        verified_at=timezone.now(),
    )
    answers = [
        ProviderVerificationResult.complete({"8"}),
        ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED),
        ProviderVerificationResult.unavailable("verification_unavailable"),
    ]

    async def provider(*args, **kwargs):
        return answers.pop(0)

    async def no_sleep(_seconds):
        return None

    async def verify():
        return await verify_connection_access(
            user.id, connection.id, {opp.id}, provider_verifier=provider, sleep=no_sleep
        )

    assert (await verify()).status == AccessVerificationStatus.VERIFIED
    await UpstreamAccessProof.objects.filter(connection=connection).aupdate(
        verified_at=timezone.now() - timedelta(minutes=10)
    )
    window = timedelta(minutes=30)
    assert await agrace_proof_tenant_ids(user.id, connection.id, {opp.id}, max_age=window)

    await verify()

    assert answers == []
    # Not archived: the 401 may only have been a stale token.
    assert await TenantMembership.objects.filter(user=user, tenant=opp).aexists()
    assert not await agrace_proof_tenant_ids(user.id, connection.id, {opp.id}, max_age=window)
    sibling_proof = await UpstreamAccessProof.objects.aget(connection=connection, tenant=sibling)
    assert sibling_proof.verified_at is None
    control = await VerificationControl.objects.aget(connection=connection)
    assert control.last_attempt_error_code == VERIFICATION_UNAVAILABLE_AFTER_REJECTION


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_denial_that_could_not_be_published_never_earns_grace(
    user, tenant, workspace, monkeypatch
):
    """The provider said "access lost" but publishing it timed out: nothing is archived,
    so the proof must not be left for grace to read as positive."""
    membership = await TenantMembership.objects.select_related("connection").aget(
        user=user, tenant=tenant
    )
    connection = membership.connection
    await _aage_proof(user, tenant, timedelta(minutes=10))

    async def omits_the_tenant(*args, **kwargs):
        return ProviderVerificationResult.complete(set())

    async def publication_times_out(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(
        access_verification_service, "apublish_verification_receipt", publication_times_out
    )

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=omits_the_tenant
    )
    await _drain_background()

    assert result.status == AccessVerificationStatus.UNAVAILABLE
    assert result.error_code == VERIFICATION_UNAVAILABLE_AFTER_DENIAL
    assert not access_freshness._grace_eligible(result)
    window = timedelta(minutes=30)
    assert not await agrace_proof_tenant_ids(user.id, connection.id, {tenant.id}, max_age=window)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda: ProviderVerificationResult.complete(set()), id="omission"),
        pytest.param(
            lambda: ProviderVerificationResult.indeterminate("verification_indeterminate"),
            id="indeterminate",
        ),
    ],
)
async def test_an_answer_short_of_access_withdraws_the_proof_before_publishing(
    user, tenant, workspace, monkeypatch, answer
):
    """A request waiting on the lease while the answer publishes must find no proof
    for grace to read as positive, and neither may any request after it."""
    membership = await TenantMembership.objects.select_related("connection").aget(
        user=user, tenant=tenant
    )
    connection = membership.connection
    await _aage_proof(user, tenant, timedelta(minutes=10))
    window = timedelta(minutes=30)
    seen_at_publication = []
    original = access_verification_service.apublish_verification_receipt

    async def provider(*args, **kwargs):
        return answer()

    async def publish(*args, **kwargs):
        seen_at_publication.append(
            await agrace_proof_tenant_ids(user.id, connection.id, {tenant.id}, max_age=window)
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(access_verification_service, "apublish_verification_receipt", publish)

    await verify_connection_access(user.id, connection.id, {tenant.id}, provider_verifier=provider)

    assert seen_at_publication == [frozenset()]
    assert not await agrace_proof_tenant_ids(user.id, connection.id, {tenant.id}, max_age=window)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_dead_refresh_grant_withdraws_the_connections_proofs(user, tenant, workspace):
    membership = await TenantMembership.objects.select_related("connection").aget(
        user=user, tenant=tenant
    )
    connection = membership.connection
    await _aage_proof(user, tenant, timedelta(minutes=10))
    window = timedelta(minutes=30)

    async def grant_revoked(*args, **kwargs):
        return ProviderVerificationResult.unavailable(ErrorCode.AUTH_TOKEN_EXPIRED)

    result = await verify_connection_access(
        user.id, connection.id, {tenant.id}, provider_verifier=grant_revoked
    )

    assert result.error_code == ErrorCode.AUTH_TOKEN_EXPIRED
    # Nothing archived: reconnecting fixes it. But a later outage finds no proof to grace.
    assert await TenantMembership.objects.filter(user=user, tenant=tenant).aexists()
    assert not await agrace_proof_tenant_ids(user.id, connection.id, {tenant.id}, max_age=window)
