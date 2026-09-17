from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace

from allauth.socialaccount.models import SocialToken
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantMembership
from apps.users.services.access_verification import (
    ClaimStatus,
    PublicationStatus,
    VerificationClaim,
    VerificationDeadlineExceeded,
    _tenant_scope,
    aclaim_verification,
    apublish_verification_receipt,
    arebase_verification_claim,
    arelease_verification,
    attempt_receipt_matches,
)
from apps.users.services.access_verification_providers import (
    NETWORK_LIMITER,
    PROVIDER_BUDGET_SECONDS,
    verify_provider,
)
from apps.users.services.access_verification_types import (
    AccessVerificationResult,
    AccessVerificationStatus,
    ProviderVerificationResult,
    VerificationOutcome,
    VerificationResult,
)
from apps.users.services.oauth_scope import canonical_provider
from apps.users.services.token_refresh import (
    TokenRefreshError,
    TokenRefreshRejected,
    TokenRefreshUnavailable,
    get_token_url,
    refresh_oauth_token_result,
    token_needs_refresh,
)

_VERIFICATION_UNAVAILABLE = "verification_unavailable"
_VERIFICATION_INDETERMINATE = "verification_indeterminate"
_VERIFICATION_IN_PROGRESS = "verification_in_progress"
_VERIFICATION_RETRY = "verification_retry"
_UPSTREAM_ACCESS_LOST = "upstream_access_lost"
_JITTER = random.SystemRandom()
_CLEANUP_WAIT_SECONDS = 0.075
_SUPERVISED_OPERATIONS: set[asyncio.Task] = set()


def _supervise(task: asyncio.Task, *, on_result=None) -> None:
    """Retain overdue ORM work and consume its result until it finishes."""
    _SUPERVISED_OPERATIONS.add(task)

    def finished(completed):
        _SUPERVISED_OPERATIONS.discard(completed)
        try:
            result = completed.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if on_result is not None:
            followup = asyncio.create_task(on_result(result))
            _supervise(followup)

    task.add_done_callback(finished)


async def _await_until(awaitable, *, deadline, clock, on_late_result=None):
    task = asyncio.ensure_future(awaitable)
    remaining = deadline - clock()
    if remaining <= 0:
        _supervise(task, on_result=on_late_result)
        raise TimeoutError
    try:
        done, _pending = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        _supervise(task, on_result=on_late_result)
        raise
    if not done:
        _supervise(task, on_result=on_late_result)
        raise TimeoutError
    return task.result()


async def _release_claim(claim) -> bool:
    try:
        return await _await_until(
            arelease_verification(claim),
            deadline=time.monotonic() + _CLEANUP_WAIT_SECONDS,
            clock=time.monotonic,
        )
    except TimeoutError:
        return False


async def _release_late_claim(claim) -> None:
    if claim is not None and claim.status == ClaimStatus.CLAIMED:
        await _release_claim(claim)


async def _claim_with_cancellation_cleanup(
    actor_user_id, connection_id, tenant_ids, *, deadline, clock
):
    cancelled = threading.Event()
    try:
        return await _await_until(
            aclaim_verification(
                actor_user_id,
                connection_id,
                tenant_ids,
                deadline=deadline,
                clock=clock,
                cancelled=cancelled,
            ),
            deadline=deadline,
            clock=clock,
            on_late_result=_release_late_claim,
        )
    except TimeoutError:
        cancelled.set()
        return VerificationClaim(ClaimStatus.DEADLINE, frozenset(tenant_ids))
    except asyncio.CancelledError:
        cancelled.set()
        raise


async def _load_claim_token(claim):
    token_id, refresh_token, app_id = claim.request.token_snapshot
    return (
        await SocialToken.objects.filter(
            pk=token_id,
            account_id=int(claim.observation.account_identity),
            app_id=app_id,
            token=claim.request.credential,
            token_secret=refresh_token,
        )
        .select_related("app")
        .afirst()
    )


async def _refresh_claim_if_needed(claim, *, deadline, clock, limiter):
    if claim.request.token_snapshot is None:
        return claim, None
    try:
        token = await _await_until(_load_claim_token(claim), deadline=deadline, clock=clock)
    except TimeoutError:
        return claim, AccessVerificationResult(
            AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
        )
    if token is None:
        await _release_claim(claim)
        return None, AccessVerificationResult(AccessVerificationStatus.RETRY, _VERIFICATION_RETRY)
    token_url = get_token_url(claim.observation.provider)
    can_refresh = bool(token_url and token.token_secret and token.app)
    if not can_refresh or not token_needs_refresh(token.expires_at, can_refresh=True):
        return claim, None
    remaining = deadline - clock()
    if remaining <= 0:
        return claim, AccessVerificationResult(
            AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
        )
    acquired = False
    try:
        await asyncio.wait_for(limiter.acquire(), timeout=remaining)
        acquired = True
        remaining = deadline - clock()
        if remaining <= 0:
            return claim, AccessVerificationResult(
                AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
            )
        refreshed = await asyncio.wait_for(
            refresh_oauth_token_result(
                token,
                token_url,
                request_timeout=remaining,
                deadline=deadline,
                clock=clock,
            ),
            timeout=remaining,
        )
    except TokenRefreshRejected:
        return claim, ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED)
    except TokenRefreshUnavailable:
        return claim, ProviderVerificationResult.unavailable(_VERIFICATION_UNAVAILABLE)
    except TokenRefreshError:
        return claim, ProviderVerificationResult.indeterminate(_VERIFICATION_INDETERMINATE)
    except TimeoutError:
        return claim, ProviderVerificationResult.unavailable(_VERIFICATION_UNAVAILABLE)
    finally:
        if acquired:
            limiter.release()
    if clock() >= deadline:
        return claim, ProviderVerificationResult.unavailable(_VERIFICATION_UNAVAILABLE)
    try:
        rebased = await _await_until(
            arebase_verification_claim(claim, refreshed.snapshot, deadline=deadline, clock=clock),
            deadline=deadline,
            clock=clock,
        )
    except (TimeoutError, VerificationDeadlineExceeded):
        await _release_claim(claim)
        return None, AccessVerificationResult(
            AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
        )
    if rebased is None:
        await _release_claim(claim)
        return None, AccessVerificationResult(AccessVerificationStatus.RETRY, _VERIFICATION_RETRY)
    return rebased, None


async def _map_provider_result(claim, result):
    # Claims and publication both match memberships on the canonical provider, so
    # the mapping sandwiched between them must too. An exact comparison misses an
    # alias tenant (commcare-custom on a commcare connection), which then never
    # reaches result.tenant_ids, and publication -- whose omission scope IS
    # canonical -- archives the membership and denies a tenant the provider just
    # confirmed. Canonicalize in Python: a provider__startswith filter would sweep
    # commcare_connect into commcare, because canonical_provider resolves
    # commcare_connect first.
    connection_provider = canonical_provider(claim.observation.provider)
    if result.outcome == VerificationOutcome.COMPLETE:
        tenant_ids = {
            tenant_id
            async for tenant_id, tenant_provider in TenantMembership.all_objects.filter(
                user_id=claim.observation.user_id,
                connection_id=claim.observation.connection_id,
                tenant__external_id__in=result.external_ids,
            ).values_list("tenant_id", "tenant__provider")
            if canonical_provider(tenant_provider) == connection_provider
        }
        return VerificationResult.complete(tenant_ids)
    if result.outcome == VerificationOutcome.CREDENTIAL_REJECTED:
        return VerificationResult.credential_rejected(result.error_code)
    if result.outcome == VerificationOutcome.TENANT_DENIED:
        tenant_id = None
        async for candidate_id, tenant_provider in TenantMembership.all_objects.filter(
            user_id=claim.observation.user_id,
            connection_id=claim.observation.connection_id,
            tenant__external_id=result.denied_external_id,
            tenant_id__in=claim.requested_tenant_ids,
        ).values_list("tenant_id", "tenant__provider"):
            if canonical_provider(tenant_provider) == connection_provider:
                tenant_id = candidate_id
                break
        if tenant_id is None:
            return VerificationResult.indeterminate(_VERIFICATION_INDETERMINATE)
        return VerificationResult.tenant_denied(tenant_id, result.error_code)
    if result.outcome == VerificationOutcome.UNAVAILABLE:
        return VerificationResult.unavailable(result.error_code)
    return VerificationResult.indeterminate(result.error_code)


def _service_result(provider_result, accepted_tenant_ids, requested_tenant_ids):
    if provider_result.outcome == VerificationOutcome.COMPLETE:
        if requested_tenant_ids.issubset(accepted_tenant_ids):
            return AccessVerificationResult(AccessVerificationStatus.VERIFIED)
        return AccessVerificationResult(AccessVerificationStatus.DENIED, _UPSTREAM_ACCESS_LOST)
    if provider_result.outcome in {
        VerificationOutcome.CREDENTIAL_REJECTED,
        VerificationOutcome.TENANT_DENIED,
    }:
        return AccessVerificationResult(AccessVerificationStatus.DENIED, provider_result.error_code)
    if provider_result.outcome == VerificationOutcome.UNAVAILABLE:
        return AccessVerificationResult(
            AccessVerificationStatus.UNAVAILABLE, provider_result.error_code
        )
    return AccessVerificationResult(
        AccessVerificationStatus.INDETERMINATE, provider_result.error_code
    )


async def _wait_for_claim(
    actor_user_id,
    connection_id,
    tenant_ids,
    initial_claim,
    *,
    deadline,
    clock,
    sleep,
):
    original_lease_token = initial_claim.lease_token
    original_observation = initial_claim.observation
    while clock() < deadline:
        await sleep(min(_JITTER.uniform(0.04, 0.08), max(0, deadline - clock())))
        claim = await _claim_with_cancellation_cleanup(
            actor_user_id,
            connection_id,
            tenant_ids,
            deadline=deadline,
            clock=clock,
        )
        if claim.status == ClaimStatus.DEADLINE:
            return AccessVerificationResult(
                AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
            )
        if claim.status != ClaimStatus.CLAIMED:
            if claim.status != ClaimStatus.IN_PROGRESS:
                return claim
            continue
        if _attempt_matches_waiter_lineage(
            claim.completed_attempt,
            original_lease_token,
            original_observation,
            claim.observation,
            tenant_ids,
        ):
            durable_result = _durable_result(claim.completed_attempt, tenant_ids)
            if durable_result is not None:
                await _release_claim(claim)
                return durable_result
        return claim
    return None


def _durable_result(receipt, requested_tenant_ids):
    """Read a matched receipt the way _service_result reads a fresh provider result.

    A COMPLETE receipt's scope is the set the attempt confirmed live, so a waiter
    inside that scope was verified, not denied.

    The COMPLETE branch is not reachable from the current waiter path -- a covered
    waiter finds its proof refreshed and returns on a FRESH claim before getting
    here, and an uncovered one fails :func:`attempt_receipt_matches`' subset test.
    It is kept so this function agrees with ``_service_result`` on identical input
    instead of contradicting it, and because it is the only branch that GRANTS
    access: the coverage check makes a caller that skipped the match fail closed,
    where a redundant DENIED only costs a re-verification.
    """
    if receipt.outcome == VerificationOutcome.COMPLETE:
        if _tenant_scope(requested_tenant_ids) <= receipt.tenant_ids:
            return AccessVerificationResult(AccessVerificationStatus.VERIFIED)
        return AccessVerificationResult(AccessVerificationStatus.DENIED, _UPSTREAM_ACCESS_LOST)
    if receipt.outcome == VerificationOutcome.CREDENTIAL_REJECTED:
        return AccessVerificationResult(
            AccessVerificationStatus.DENIED,
            receipt.error_code or ErrorCode.AUTH_TOKEN_EXPIRED,
        )
    if receipt.outcome == VerificationOutcome.TENANT_DENIED:
        # The scope records the denied tenants, so a match means every tenant the
        # waiter asked for was denied upstream.
        return AccessVerificationResult(
            AccessVerificationStatus.DENIED,
            receipt.error_code or _UPSTREAM_ACCESS_LOST,
        )
    if receipt.outcome == VerificationOutcome.UNAVAILABLE:
        return AccessVerificationResult(
            AccessVerificationStatus.UNAVAILABLE,
            receipt.error_code or _VERIFICATION_UNAVAILABLE,
        )
    if receipt.outcome == VerificationOutcome.INDETERMINATE:
        return AccessVerificationResult(
            AccessVerificationStatus.INDETERMINATE,
            receipt.error_code or _VERIFICATION_INDETERMINATE,
        )
    return None


# Outcomes whose verdict belongs to specific tenants rather than to the whole
# connection. Only these may be gated on the receipt's recorded tenant scope.
_TENANT_SCOPED_OUTCOMES = frozenset(
    {VerificationOutcome.COMPLETE, VerificationOutcome.TENANT_DENIED}
)


def _attempt_matches_waiter_lineage(
    receipt, lease_token, original, current, requested_tenant_ids
) -> bool:
    observations = [original, current]
    if (
        receipt is not None
        and receipt.outcome == VerificationOutcome.CREDENTIAL_REJECTED
        and original is not None
        and current is not None
    ):
        observations.append(replace(current, upstream_denied_at=original.upstream_denied_at))
    # A rejected credential, an unreachable provider or an indeterminate answer
    # applies to every tenant on the connection, so a waiter asking about other
    # tenants may still reuse it and skip a second upstream discovery. Passing the
    # receipt's own scope satisfies attempt_receipt_matches' subset test without
    # weakening its lease and observation checks, which are the real gates here.
    # A legacy receipt carries an empty scope and still matches nothing.
    scope = (
        requested_tenant_ids
        if receipt is not None and receipt.outcome in _TENANT_SCOPED_OUTCOMES
        else getattr(receipt, "tenant_ids", frozenset())
    )
    return any(
        attempt_receipt_matches(receipt, lease_token, observation, scope)
        for observation in observations
    )


async def verify_connection_access(
    actor_user_id,
    connection_id,
    tenant_ids: Iterable,
    *,
    deadline: float | None = None,
    provider_verifier: Callable[..., Awaitable[ProviderVerificationResult]] = verify_provider,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limiter: asyncio.Semaphore = NETWORK_LIMITER,
) -> AccessVerificationResult:
    """Verify one connection once and publish only its known membership history."""
    requested = frozenset(tenant_ids)
    if not requested:
        return AccessVerificationResult(
            AccessVerificationStatus.DENIED, ErrorCode.AUTH_CREDENTIAL_MISSING
        )
    deadline = min(
        deadline if deadline is not None else float("inf"),
        clock() + PROVIDER_BUDGET_SECONDS,
    )
    claim = await _claim_with_cancellation_cleanup(
        actor_user_id,
        connection_id,
        requested,
        deadline=deadline,
        clock=clock,
    )
    if claim.status == ClaimStatus.FRESH:
        return AccessVerificationResult(AccessVerificationStatus.VERIFIED)
    if claim.status == ClaimStatus.DENIED:
        return AccessVerificationResult(
            AccessVerificationStatus.DENIED, ErrorCode.AUTH_CREDENTIAL_MISSING
        )
    if claim.status == ClaimStatus.DEADLINE:
        return AccessVerificationResult(
            AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
        )
    if claim.status == ClaimStatus.IN_PROGRESS:
        claim = await _wait_for_claim(
            actor_user_id,
            connection_id,
            requested,
            claim,
            deadline=deadline,
            clock=clock,
            sleep=sleep,
        )
        if claim is None:
            return AccessVerificationResult(
                AccessVerificationStatus.IN_PROGRESS, _VERIFICATION_IN_PROGRESS
            )
        if isinstance(claim, AccessVerificationResult):
            return claim
        if claim.status == ClaimStatus.FRESH:
            return AccessVerificationResult(AccessVerificationStatus.VERIFIED)
        if claim.status == ClaimStatus.DENIED:
            return AccessVerificationResult(
                AccessVerificationStatus.DENIED, ErrorCode.AUTH_CREDENTIAL_MISSING
            )
    try:
        claim, early_result = await _refresh_claim_if_needed(
            claim, deadline=deadline, clock=clock, limiter=limiter
        )
        if claim is None:
            return early_result
        if isinstance(early_result, AccessVerificationResult):
            await _release_claim(claim)
            return early_result
        if isinstance(early_result, ProviderVerificationResult):
            provider_result = early_result
        else:
            if clock() >= deadline:
                provider_result = ProviderVerificationResult.unavailable(_VERIFICATION_UNAVAILABLE)
            else:
                try:
                    provider_result = await asyncio.wait_for(
                        provider_verifier(claim.request, deadline=deadline),
                        timeout=max(0, deadline - clock()),
                    )
                except TimeoutError:
                    provider_result = ProviderVerificationResult.unavailable(
                        _VERIFICATION_UNAVAILABLE
                    )
        completed_at = timezone.now()
        try:
            mapped = await _await_until(
                _map_provider_result(claim, provider_result), deadline=deadline, clock=clock
            )
        except TimeoutError:
            await _release_claim(claim)
            return AccessVerificationResult(
                AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
            )
        if clock() >= deadline:
            await _release_claim(claim)
            return AccessVerificationResult(
                AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
            )
        try:
            publication = await _await_until(
                apublish_verification_receipt(
                    claim,
                    mapped,
                    verified_at=completed_at,
                    deadline=deadline,
                    clock=clock,
                ),
                deadline=deadline,
                clock=clock,
            )
        except TimeoutError:
            await _release_claim(claim)
            return AccessVerificationResult(
                AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
            )
        if publication.status == PublicationStatus.TIMED_OUT or clock() >= deadline:
            await _release_claim(claim)
            return AccessVerificationResult(
                AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_UNAVAILABLE
            )
        if publication.status != PublicationStatus.PUBLISHED:
            return AccessVerificationResult(AccessVerificationStatus.RETRY, _VERIFICATION_RETRY)
        return _service_result(provider_result, publication.accepted_tenant_ids, requested)
    except asyncio.CancelledError:
        await asyncio.shield(_release_claim(claim))
        raise
    except Exception:
        await asyncio.shield(_release_claim(claim))
        raise
