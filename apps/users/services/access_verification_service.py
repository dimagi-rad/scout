from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from allauth.socialaccount.models import SocialToken
from asgiref.sync import SyncToAsync, ThreadSensitiveContext
from django.db import close_old_connections
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
from apps.users.services.oauth_scope import amemberships_on_provider, canonical_provider
from apps.users.services.token_refresh import (
    PersistedTokenSnapshot,
    TokenRefreshError,
    TokenRefreshRejected,
    TokenRefreshUnavailable,
    ensure_usable_credential,
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
# The incident's 401 came ~170ms after the refresh; 0.5s covers that with margin
# while staying small against the 10s interactive budget.
_REJECTION_RETRY_PAUSE_SECONDS = 0.5
logger = logging.getLogger(__name__)
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


async def _load_stored_token(claim):
    """The claim's token row as stored now, whatever credential it holds."""
    token_id, _refresh_token, app_id = claim.request.token_snapshot
    return (
        await SocialToken.objects.filter(
            pk=token_id,
            account_id=int(claim.observation.account_identity),
            app_id=app_id,
        )
        .select_related("app")
        .afirst()
    )


def _holds_claim_credential(token, claim) -> bool:
    return bool(
        token is not None
        and claim.request.credential
        and token.token == claim.request.credential
        and token.token_secret == claim.request.token_snapshot[1]
    )


async def _load_claim_token(claim):
    token = await _load_stored_token(claim)
    return token if _holds_claim_credential(token, claim) else None


def _refresh_url(claim, token) -> str | None:
    token_url = get_token_url(claim.observation.provider, claim.observation.scope_key)
    return token_url if token_url and token.token_secret and token.app else None


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
    token_url = _refresh_url(claim, token)
    if not token_url or not token_needs_refresh(token.expires_at, can_refresh=True):
        return claim, None
    return await _refresh_and_rebase(
        claim, token, token_url, deadline=deadline, clock=clock, limiter=limiter
    )


async def _refresh_and_rebase(claim, token, token_url, *, deadline, clock, limiter):
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
        # Both wrappers around refresh_oauth_token_result apply this before handing
        # back a snapshot, and so must we. A CAS loss that did not advance the
        # credential returns SUPERSEDED with the STORED token -- the one this refresh
        # just rotated away upstream. Rebasing would succeed, because the row really
        # is unchanged, and we would then verify with a credential already dead at
        # the provider, earning a 401 that archives every membership.
        ensure_usable_credential(refreshed)
    except TokenRefreshRejected:
        # Reached two ways: an outright invalid_grant from the token endpoint, and the
        # unusable-credential check above. Either way a dead refresh grant requires
        # reconnect but does not prove resource access loss, which is what
        # TokenRefreshRejected.denial_handled records. Publishing CREDENTIAL_REJECTED
        # here would call record_validated_upstream_denial with tenant_id=None and
        # archive every live membership on the connection, so one failed refresh would
        # revoke every tenant without any resource call denying access. A genuine
        # provider 401 is the case that legitimately archives; see _status_result.
        # AUTH_TOKEN_EXPIRED still travels to the caller as the reconnect signal, and
        # publication leaves memberships and proofs untouched.
        return claim, ProviderVerificationResult.unavailable(ErrorCode.AUTH_TOKEN_EXPIRED)
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
    return await _rebase_claim(claim, refreshed.snapshot, deadline=deadline, clock=clock)


async def _rebase_claim(claim, snapshot, *, deadline, clock):
    try:
        rebased = await _await_until(
            arebase_verification_claim(claim, snapshot, deadline=deadline, clock=clock),
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


async def _renew_after_rejection(claim, *, refreshed, deadline, clock, limiter):
    """Put a current OAuth credential on the claim before retrying a provider 401 once.

    A 401 against a stale or expired access token says nothing about access, so it
    must not reach publication, which archives every membership on the connection.
    Only a credential that is current at the provider -- rotated by a concurrent
    refresh, refreshed here, or the one this attempt just refreshed -- earns the
    retry whose 401 is authoritative. Returns ``(claim, early_result)`` like
    :func:`_refresh_claim_if_needed`; ``early_result`` None means retry the provider.
    """
    try:
        token = await _await_until(_load_stored_token(claim), deadline=deadline, clock=clock)
    except TimeoutError:
        return claim, ProviderVerificationResult.unavailable(_VERIFICATION_UNAVAILABLE)
    if token is None or not token.token:
        await _release_claim(claim)
        return None, AccessVerificationResult(AccessVerificationStatus.RETRY, _VERIFICATION_RETRY)
    if not _holds_claim_credential(token, claim):
        logger.info(
            "Retrying upstream verification for connection %s with a concurrently "
            "rotated credential after HTTP 401",
            claim.observation.connection_id,
        )
        return await _rebase_claim(
            claim,
            PersistedTokenSnapshot(
                token_id=token.pk,
                account_id=token.account_id,
                app_id=token.app_id,
                access_token=token.token,
                refresh_token=token.token_secret,
                expires_at=token.expires_at,
            ),
            deadline=deadline,
            clock=clock,
        )
    token_url = _refresh_url(claim, token)
    if not refreshed and token_url:
        logger.info(
            "Refreshing the credential for connection %s after HTTP 401 before deciding access",
            claim.observation.connection_id,
        )
        return await _refresh_and_rebase(
            claim, token, token_url, deadline=deadline, clock=clock, limiter=limiter
        )
    if (
        not refreshed
        and token.expires_at is not None
        and token_needs_refresh(token.expires_at, can_refresh=False)
    ):
        # Expired (or about to be) and unrenewable: only a reconnect helps, and it
        # proves nothing about access.
        return claim, ProviderVerificationResult.unavailable(ErrorCode.AUTH_TOKEN_EXPIRED)
    return claim, None


async def _requested_external_ids(claim) -> frozenset[str]:
    """External ids of the claimed tenants, for providers that can check just those."""
    if canonical_provider(claim.observation.provider) != "commcare_connect":
        return frozenset()
    return frozenset(
        [
            external_id
            async for external_id in TenantMembership.all_objects.filter(
                user_id=claim.observation.user_id,
                connection_id=claim.observation.connection_id,
                tenant_id__in=claim.requested_tenant_ids,
            ).values_list("tenant__external_id", flat=True)
        ]
    )


async def _call_provider(provider_verifier, claim, *, deadline, clock):
    if clock() >= deadline:
        return ProviderVerificationResult.unavailable(_VERIFICATION_UNAVAILABLE)
    try:
        external_ids = await _await_until(
            _requested_external_ids(claim), deadline=deadline, clock=clock
        )
        return await asyncio.wait_for(
            provider_verifier(claim.request, deadline=deadline, external_ids=external_ids),
            timeout=max(0, deadline - clock()),
        )
    except TimeoutError:
        return ProviderVerificationResult.unavailable(_VERIFICATION_UNAVAILABLE)


async def _map_provider_result(claim, result):
    # Claims and publication both match memberships on the canonical provider, so
    # the mapping sandwiched between them must too, or publication archives an
    # alias tenant the provider just confirmed.
    provider = claim.observation.provider
    if result.outcome == VerificationOutcome.COMPLETE:
        owned = TenantMembership.all_objects.filter(
            user_id=claim.observation.user_id,
            connection_id=claim.observation.connection_id,
        )
        scope = None
        memberships = owned.filter(tenant__external_id__in=result.external_ids)
        if result.scoped:
            scope = await amemberships_on_provider(
                owned.filter(
                    tenant__external_id__in=result.scope,
                    tenant_id__in=claim.requested_tenant_ids,
                ),
                provider,
                "tenant_id",
            )
            memberships = memberships.filter(tenant_id__in=scope)
        tenant_ids = await amemberships_on_provider(memberships, provider, "tenant_id")
        return VerificationResult.complete(set(tenant_ids), scope=scope)
    if result.outcome == VerificationOutcome.CREDENTIAL_REJECTED:
        return VerificationResult.credential_rejected(result.error_code)
    if result.outcome == VerificationOutcome.TENANT_DENIED:
        candidates = await amemberships_on_provider(
            TenantMembership.all_objects.filter(
                user_id=claim.observation.user_id,
                connection_id=claim.observation.connection_id,
                tenant__external_id=result.denied_external_id,
                tenant_id__in=claim.requested_tenant_ids,
            ),
            provider,
            "tenant_id",
        )
        if not candidates:
            return VerificationResult.indeterminate(_VERIFICATION_INDETERMINATE)
        return VerificationResult.tenant_denied(candidates[0], result.error_code)
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
    return any(
        attempt_receipt_matches(receipt, lease_token, observation, requested_tenant_ids)
        for observation in observations
    )


# Every detached verification's ORM work runs on this context's one dedicated thread.
# Not the caller's thread: a sync (DRF) view reaches here through async_to_sync, whose
# executor dies when that call returns. Not asgiref's process-wide default either: a
# sync caller blocked waiting on the check may be holding that very thread.
class _RecyclingExecutor(ThreadPoolExecutor):
    """Closes each call's connection when done, as a request's end would.

    No request signal ever fires on these threads, and with CONN_MAX_AGE at 0 a
    connection is obsolete as soon as its call returns: left open, each thread would
    hold an idle connection (prod and staging share one RDS), and one dropped by a
    failover would fail every later check that lands on it.
    """

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(self._recycled, fn, *args, **kwargs)

    @staticmethod
    def _recycled(fn, *args, **kwargs):
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()


_DETACHED_ORM_CONTEXT = ThreadSensitiveContext()
# A small pool, so one publication waiting on a row lock cannot hold up the rest.
SyncToAsync.context_to_thread_executor[_DETACHED_ORM_CONTEXT] = _RecyclingExecutor(
    max_workers=4, thread_name_prefix="access-verification"
)


def _spawn_detachable(coroutine) -> asyncio.Task:
    """A task that may outlive its caller, with its own context and ORM threads."""
    context = contextvars.Context()
    context.run(SyncToAsync.thread_sensitive_context.set, _DETACHED_ORM_CONTEXT)
    return asyncio.get_running_loop().create_task(coroutine, context=context)


def _detach(verification, connection_id) -> None:
    """Supervise a verification nobody is waiting for, and report what it could not do."""

    def report(done):
        if done.cancelled():
            return
        if done.exception() is not None:
            logger.warning(
                "Background upstream verification failed for connection %s",
                connection_id,
                exc_info=done.exception(),
            )
        elif done.result().status not in (
            AccessVerificationStatus.VERIFIED,
            AccessVerificationStatus.DENIED,
        ):
            logger.warning(
                "Background upstream verification unconfirmed for connection %s: "
                "status=%s error_code=%s",
                connection_id,
                done.result().status,
                done.result().error_code or "-",
            )

    verification.add_done_callback(report)
    _supervise(verification)


async def _verify_and_publish(
    claim,
    requested,
    early_provider_result,
    *,
    refreshed,
    deadline,
    clock,
    provider_verifier,
    sleep,
    limiter,
) -> AccessVerificationResult:
    """Ask the provider (unless refresh already decided), then publish under ``claim``.

    Owns the lease from here on: whatever happens, the lease ends published or
    released, so this can outlive a caller that stopped waiting for it.
    """
    try:
        if early_provider_result is not None:
            provider_result = early_provider_result
        else:
            provider_result = await _call_provider(
                provider_verifier, claim, deadline=deadline, clock=clock
            )
            if (
                provider_result.outcome == VerificationOutcome.CREDENTIAL_REJECTED
                and claim.request.token_snapshot is not None
            ):
                claim, early_result = await _renew_after_rejection(
                    claim,
                    refreshed=refreshed,
                    deadline=deadline,
                    clock=clock,
                    limiter=limiter,
                )
                if claim is None:
                    return early_result
                if isinstance(early_result, AccessVerificationResult):
                    await _release_claim(claim)
                    return early_result
                if isinstance(early_result, ProviderVerificationResult):
                    provider_result = early_result
                else:
                    # Whichever credential the retry carries was usually issued moments
                    # ago, and a provider can briefly reject a token it just issued.
                    await sleep(min(_REJECTION_RETRY_PAUSE_SECONDS, max(0, deadline - clock())))
                    provider_result = await _call_provider(
                        provider_verifier, claim, deadline=deadline, clock=clock
                    )
                    if provider_result.outcome == VerificationOutcome.CREDENTIAL_REJECTED:
                        logger.warning(
                            "Provider rejected the current credential for connection %s twice; "
                            "recording the denial",
                            claim.observation.connection_id,
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


async def verify_connection_access(
    actor_user_id,
    connection_id,
    tenant_ids: Iterable,
    *,
    deadline: float | None = None,
    respond_by: float | None = None,
    provider_verifier: Callable[..., Awaitable[ProviderVerificationResult]] = verify_provider,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limiter: asyncio.Semaphore = NETWORK_LIMITER,
) -> AccessVerificationResult:
    """Verify one connection once and publish only its known membership history.

    ``deadline`` bounds the whole verification. ``respond_by`` (default: the
    deadline) is when the caller needs an answer: a provider check still running
    then keeps going in the background and publishes its proof, holding the lease
    so concurrent callers coalesce on it, and this call answers IN_PROGRESS.
    """
    requested = frozenset(tenant_ids)
    if not requested:
        return AccessVerificationResult(
            AccessVerificationStatus.DENIED, ErrorCode.AUTH_CREDENTIAL_MISSING
        )
    deadline = min(
        deadline if deadline is not None else float("inf"),
        clock() + PROVIDER_BUDGET_SECONDS,
    )
    respond_by = min(respond_by if respond_by is not None else deadline, deadline)
    claim = await _claim_with_cancellation_cleanup(
        actor_user_id,
        connection_id,
        requested,
        deadline=respond_by,
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
            deadline=respond_by,
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
        claimed = claim
        claim, early_result = await _refresh_claim_if_needed(
            claim, deadline=respond_by, clock=clock, limiter=limiter
        )
        if claim is None:
            return early_result
        if isinstance(early_result, AccessVerificationResult):
            await _release_claim(claim)
            return early_result
    except asyncio.CancelledError:
        await asyncio.shield(_release_claim(claim))
        raise
    except Exception:
        await asyncio.shield(_release_claim(claim))
        raise
    verification = _spawn_detachable(
        _verify_and_publish(
            claim,
            requested,
            early_result,
            refreshed=claim is not claimed,
            deadline=deadline,
            clock=clock,
            provider_verifier=provider_verifier,
            sleep=sleep,
            limiter=limiter,
        )
    )
    if respond_by >= deadline:
        # Bounded by the deadline itself; a cancelled caller cancels it, and waits
        # for it to release the lease.
        try:
            return await verification
        except asyncio.CancelledError:
            # Cancelled before its first step, it never reached the code that would.
            await asyncio.shield(_release_claim(claim))
            raise
    try:
        done, _pending = await asyncio.wait({verification}, timeout=max(0, respond_by - clock()))
    except asyncio.CancelledError:
        # The verification owns the lease and stays bounded by ``deadline``; a
        # caller going away must not throw away a provider answer already paid for.
        _detach(verification, connection_id)
        raise
    if not done:
        _detach(verification, connection_id)
        return AccessVerificationResult(
            AccessVerificationStatus.IN_PROGRESS, _VERIFICATION_IN_PROGRESS
        )
    return verification.result()
