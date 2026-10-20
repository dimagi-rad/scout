"""Upstream-freshness admission for protected workspace access.

``apps.workspaces.access`` decides from local state whether a member may use a
workspace. This layer adds the freshness policy (#548): every live tenant
membership the decision relies on must hold an upstream proof younger than five
minutes. A stale proof is rechecked through the connection-elected verification
service, so concurrent callers share one provider round-trip.

Outcomes: fresh → admit with no provider call; authoritative revocation → deny,
with the membership archival already persisted by the verification publisher;
provider unavailable → a retryable denial (503) that leaves every membership in
place; an indeterminate answer → a non-retryable denial that also removes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from asgiref.sync import SyncToAsync, async_to_sync
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantMembership
from apps.users.services.access_verification import (
    PROOF_RENEW_AFTER,
    afresh_proof_tenant_ids,
    afresh_proof_verified_at,
    agrace_proof_tenant_ids,
    fresh_proof_verified_at,
    grace_proof_tenant_ids,
)
from apps.users.services.access_verification_providers import (
    PROVIDER_BUDGET_SECONDS,
    RENEWAL_LIMITER,
)
from apps.users.services.access_verification_service import (
    schedule_background_verification,
    verify_connection_access,
)
from apps.users.services.access_verification_types import (
    AccessVerificationResult,
    AccessVerificationStatus,
)

logger = logging.getLogger(__name__)


class VerificationBudget(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


# How long a caller waits for an answer: an interactive caller is a person waiting on
# a response; a worker can afford the provider adapter's full budget. Either way it is
# end-to-end across every connection being rechecked, never multiplied per connection.
BUDGET_SECONDS = {
    VerificationBudget.INTERACTIVE: 10.0,
    VerificationBudget.BACKGROUND: PROVIDER_BUDGET_SECONDS,
}

CREDENTIAL_MISSING = "credential_missing"
CREDENTIAL_EXPIRED = "credential_expired"
UPSTREAM_ACCESS_LOST = "upstream_access_lost"
VERIFICATION_INDETERMINATE = "verification_indeterminate"
VERIFICATION_UNAVAILABLE = "verification_unavailable"
VERIFICATION_IN_PROGRESS = "verification_in_progress"
# An unexpected exception during a recheck: reported as unavailable, never graced.
_VERIFICATION_FAILED = "verification_failed"

FRESHNESS_DENIAL_REASONS = (
    CREDENTIAL_MISSING,
    CREDENTIAL_EXPIRED,
    UPSTREAM_ACCESS_LOST,
    VERIFICATION_UNAVAILABLE,
    VERIFICATION_IN_PROGRESS,
    # Last: it proves nothing, so a sibling check that is still running and may yet
    # pass keeps the request retryable.
    VERIFICATION_INDETERMINATE,
)
RETRYABLE_REASONS = frozenset({VERIFICATION_UNAVAILABLE, VERIFICATION_IN_PROGRESS})
_UNCONFIRMED_STATUSES = frozenset(
    {
        AccessVerificationStatus.UNAVAILABLE,
        AccessVerificationStatus.INDETERMINATE,
        AccessVerificationStatus.IN_PROGRESS,
        AccessVerificationStatus.RETRY,
    }
)

# Registry codes for surfaces that report per-source failures (worker summaries,
# MCP envelopes); reuse the codes whose remedies already exist.
FRESHNESS_ERROR_CODES: dict[str, ErrorCode] = {
    CREDENTIAL_MISSING: ErrorCode.AUTH_CREDENTIAL_MISSING,
    CREDENTIAL_EXPIRED: ErrorCode.AUTH_TOKEN_EXPIRED,
    UPSTREAM_ACCESS_LOST: ErrorCode.AUTH_ACCESS_DENIED,
    # Nothing was removed, so workers treat it like an outage they may pass over.
    VERIFICATION_INDETERMINATE: ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE,
    VERIFICATION_UNAVAILABLE: ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE,
    VERIFICATION_IN_PROGRESS: ErrorCode.ACCESS_VERIFICATION_UNAVAILABLE,
}


def freshness_enforced() -> bool:
    return bool(getattr(settings, "UPSTREAM_ACCESS_FRESHNESS_ENFORCED", False))


@dataclass(frozen=True)
class FreshnessCheck:
    """Database-only view of which live memberships lack a fresh proof."""

    stale: dict[UUID, frozenset] = field(default_factory=dict)
    unbound: frozenset = frozenset()
    # Connections whose proofs are all fresh but at least one is due for renewal.
    renewable: dict[UUID, frozenset] = field(default_factory=dict)

    @property
    def fresh(self) -> bool:
        return not self.stale and not self.unbound


@dataclass(frozen=True)
class UpstreamAdmission:
    admitted: bool
    # True once a provider was contacted. Publication may then have archived
    # memberships or replaced proofs, so the caller must re-read local access.
    rechecked: bool = False
    reason: str | None = None
    # Connections admitted under grace: their recheck could not reach the provider,
    # and a positive proof younger than the grace window stands in for a fresh one.
    graced: frozenset = frozenset()


ADMITTED = UpstreamAdmission(admitted=True)


def _group_by_connection(rows) -> tuple[dict[UUID, set], set]:
    grouped: dict[UUID, set] = defaultdict(set)
    unbound: set = set()
    for tenant_id, connection_id in rows:
        if connection_id is None:
            unbound.add(tenant_id)
        else:
            grouped[connection_id].add(tenant_id)
    return grouped, unbound


def _live_bindings_queryset(user_id, tenant_ids):
    return TenantMembership.objects.filter(user_id=user_id, tenant_id__in=tenant_ids).values_list(
        "tenant_id", "connection_id"
    )


def _freshness_check(grouped, verified_at_by_connection, unbound, *, now) -> FreshnessCheck:
    stale, renewable = {}, {}
    renew_before = now - PROOF_RENEW_AFTER
    for connection_id, ids in grouped.items():
        ids = frozenset(ids)
        verified_at = verified_at_by_connection[connection_id]
        if verified_at.keys() != ids:
            stale[connection_id] = ids
        elif min(verified_at.values()) <= renew_before:
            renewable[connection_id] = ids
    return FreshnessCheck(stale=stale, unbound=frozenset(unbound), renewable=renewable)


def check_freshness(user_id, tenant_ids) -> FreshnessCheck:
    grouped, unbound = _group_by_connection(_live_bindings_queryset(user_id, list(tenant_ids)))
    now = timezone.now()
    verified_at = {
        connection_id: fresh_proof_verified_at(user_id, connection_id, ids, now=now)
        for connection_id, ids in grouped.items()
    }
    return _freshness_check(grouped, verified_at, unbound, now=now)


async def acheck_freshness(user_id, tenant_ids) -> FreshnessCheck:
    rows = [row async for row in _live_bindings_queryset(user_id, list(tenant_ids))]
    grouped, unbound = _group_by_connection(rows)
    now = timezone.now()
    verified_at = {
        connection_id: await afresh_proof_verified_at(user_id, connection_id, ids, now=now)
        for connection_id, ids in grouped.items()
    }
    return _freshness_check(grouped, verified_at, unbound, now=now)


async def acheck_freshness_many(user_id, tenant_ids_by_key: dict) -> dict:
    """:func:`acheck_freshness` for several tenant sets, keyed as given.

    One binding read and one proof read per connection, however many sets share
    it, so a listing's cost follows the user's connections, not its workspaces.
    It never fills ``renewable``: a listing is not an admission and renews nothing.
    """
    all_tenant_ids = {tenant_id for ids in tenant_ids_by_key.values() for tenant_id in ids}
    rows = [row async for row in _live_bindings_queryset(user_id, list(all_tenant_ids))]
    grouped, _unbound = _group_by_connection(rows)
    fresh = {
        connection_id: await afresh_proof_tenant_ids(user_id, connection_id, ids)
        for connection_id, ids in grouped.items()
    }
    rows_by_tenant = defaultdict(list)
    for tenant_id, connection_id in rows:
        rows_by_tenant[tenant_id].append((tenant_id, connection_id))
    checks = {}
    for key, ids in tenant_ids_by_key.items():
        own, unbound = _group_by_connection(
            row for tenant_id in set(ids) for row in rows_by_tenant.get(tenant_id, ())
        )
        stale = {
            connection_id: frozenset(tenant_ids)
            for connection_id, tenant_ids in own.items()
            if not tenant_ids <= fresh[connection_id]
        }
        checks[key] = FreshnessCheck(stale=stale, unbound=frozenset(unbound))
    return checks


def denial_reason(result: AccessVerificationResult) -> str | None:
    """Map the verification service's internal result onto one public reason."""
    status = result.status
    if status == AccessVerificationStatus.VERIFIED:
        return None
    if result.error_code == ErrorCode.AUTH_TOKEN_EXPIRED:
        # A dead refresh grant surfaces as UNAVAILABLE so it archives nothing, but
        # only reconnecting fixes it; retrying would just fail the same way.
        return CREDENTIAL_EXPIRED
    if status == AccessVerificationStatus.DENIED:
        if result.error_code == ErrorCode.AUTH_CREDENTIAL_MISSING:
            return CREDENTIAL_MISSING
        return UPSTREAM_ACCESS_LOST
    if status in (AccessVerificationStatus.IN_PROGRESS, AccessVerificationStatus.RETRY):
        return VERIFICATION_IN_PROGRESS
    if status == AccessVerificationStatus.INDETERMINATE:
        # The provider answered, but not in a form that proves or denies access
        # (e.g. a CommCare 403 on the domain listing); retrying gets the same answer.
        return VERIFICATION_INDETERMINATE
    return VERIFICATION_UNAVAILABLE


def most_severe(reasons) -> str | None:
    present = set(reasons)
    return next((reason for reason in FRESHNESS_DENIAL_REASONS if reason in present), None)


async def _averify_stale(user_id, stale: dict, budget: VerificationBudget) -> list:
    started = time.monotonic()
    # The provider check always gets the full background budget; an interactive
    # caller stops waiting earlier and the check finishes and publishes behind it.
    deadline = started + PROVIDER_BUDGET_SECONDS
    respond_by = started + BUDGET_SECONDS[budget]
    results = await asyncio.gather(
        *(
            verify_connection_access(
                user_id, connection_id, tenant_ids, deadline=deadline, respond_by=respond_by
            )
            for connection_id, tenant_ids in stale.items()
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
    # One connection's unexpected failure (e.g. a deadlock) must neither 500 the
    # access check nor abandon its siblings mid-lease; it is an unconfirmed check.
    for connection_id, result in zip(stale, results, strict=True):
        if isinstance(result, Exception):
            logger.warning(
                "Upstream verification failed unexpectedly for user %s connection %s",
                user_id,
                connection_id,
                exc_info=result,
            )
        elif result.status in _UNCONFIRMED_STATUSES:
            # Most of these never reach the provider (lease wait, token refresh,
            # publication), so the provider adapter's own WARNING is absent for them.
            logger.warning(
                "Upstream verification unconfirmed: status=%s error_code=%s budget=%s "
                "batch_elapsed_ms=%d budget_ms=%d user_id=%s connection_id=%s",
                result.status,
                result.error_code or "-",
                budget,
                round((time.monotonic() - started) * 1000),
                round(BUDGET_SECONDS[budget] * 1000),
                user_id,
                connection_id,
            )
    return [
        AccessVerificationResult(AccessVerificationStatus.UNAVAILABLE, _VERIFICATION_FAILED)
        if isinstance(result, Exception)
        else result
        for result in results
    ]


def grace_window():
    return timedelta(seconds=max(0, getattr(settings, "UPSTREAM_ACCESS_GRACE_SECONDS", 0)))


def _grace_eligible(result: AccessVerificationResult) -> bool:
    """Only a check that never got an answer: timeout, network, 5xx, 429, or one
    still running. Never a denial (401, omission, no-access 404), an indeterminate
    answer, a dead refresh grant, or an unexpected failure.
    """
    # Exact codes: one that saw a 401 it could not settle carries its own code.
    return (result.status, result.error_code) in {
        (AccessVerificationStatus.IN_PROGRESS, VERIFICATION_IN_PROGRESS),
        (AccessVerificationStatus.UNAVAILABLE, VERIFICATION_UNAVAILABLE),
    }


async def _agrace_admissible(user_id, connection_id, tenant_ids) -> bool:
    window = grace_window()
    if not window:
        return False
    graced = await agrace_proof_tenant_ids(user_id, connection_id, tenant_ids, max_age=window)
    return graced == frozenset(tenant_ids)


async def _aapply_grace(user_id, stale: dict, results: list) -> tuple[list, frozenset]:
    """Admit each unreachable connection whose tenants all hold a recent positive proof.

    Each one is re-verified in the background, so a revocation found there is
    published and blocks the next request.
    """
    graced = set()
    admitted = []
    for (connection_id, tenant_ids), result in zip(stale.items(), results, strict=True):
        if _grace_eligible(result) and await _agrace_admissible(user_id, connection_id, tenant_ids):
            # Even behind a running check: this joins it as a waiter, and runs its
            # own check if that one has finished without covering these tenants.
            started = schedule_background_verification(user_id, connection_id, tenant_ids)
            logger.info(
                "upstream_access_grace user_id=%s connection_id=%s tenants=%d "
                "status=%s background_recheck=%s",
                user_id,
                connection_id,
                len(tenant_ids),
                result.status.value,
                "started" if started else "running",
                extra={
                    "user_id": user_id,
                    "connection_id": str(connection_id),
                    "verification_status": result.status.value,
                },
            )
            graced.add(connection_id)
            result = AccessVerificationResult(AccessVerificationStatus.VERIFIED)
        admitted.append(result)
    return admitted, frozenset(graced)


async def _averify_stale_with_grace(
    user_id, stale: dict, budget: VerificationBudget, *, allow_grace: bool = True
):
    results = await _averify_stale(user_id, stale, budget)
    if budget != VerificationBudget.INTERACTIVE or not allow_grace:
        return results, frozenset()
    return await _aapply_grace(user_id, stale, results)


def excuse_graced(user_id, final: FreshnessCheck, graced) -> FreshnessCheck:
    """``final`` without the graced connections whose grace proofs still hold.

    Re-read rather than trusted from the admission: a revocation published in
    between must deny this very request.
    """
    window = grace_window()
    stale = {
        connection_id: ids
        for connection_id, ids in final.stale.items()
        if not (
            window
            and connection_id in graced
            and grace_proof_tenant_ids(user_id, connection_id, ids, max_age=window) == ids
        )
    }
    return FreshnessCheck(stale=stale, unbound=final.unbound)


async def aexcuse_graced(user_id, final: FreshnessCheck, graced) -> FreshnessCheck:
    stale = {}
    for connection_id, ids in final.stale.items():
        if connection_id in graced and await _agrace_admissible(user_id, connection_id, ids):
            continue
        stale[connection_id] = ids
    return FreshnessCheck(stale=stale, unbound=final.unbound)


def _renewal_due(check: FreshnessCheck, budget: VerificationBudget) -> bool:
    # Only interactive admissions renew: a worker can afford to wait for a recheck.
    return budget == VerificationBudget.INTERACTIVE and bool(check.renewable) and not check.unbound


# A renewal that could not reach the provider leaves the proofs due; without this every
# request until they expire would retry it, each taking a RENEWAL_LIMITER permit.
RENEWAL_COOLDOWN_SECONDS = 60.0
_RENEWAL_ATTEMPTS: dict[tuple, float] = {}


def _renewal_cooling_down(key, now) -> bool:
    for stale_key in [
        k for k, at in _RENEWAL_ATTEMPTS.items() if now - at >= RENEWAL_COOLDOWN_SECONDS
    ]:
        del _RENEWAL_ATTEMPTS[stale_key]
    return key in _RENEWAL_ATTEMPTS


def _schedule_renewals(user_id, check: FreshnessCheck) -> None:
    """Renew in the background the fresh proofs this admission found due (#926).

    Must run on the server's long-lived loop. At most one attempt per tenant set per
    ``RENEWAL_COOLDOWN_SECONDS`` in each process.
    """
    for connection_id, tenant_ids in check.renewable.items():
        key = (user_id, connection_id, tenant_ids)
        now = time.monotonic()
        if _renewal_cooling_down(key, now):
            continue
        if schedule_background_verification(
            user_id,
            connection_id,
            tenant_ids,
            fresh_max_age=PROOF_RENEW_AFTER,
            limiter=RENEWAL_LIMITER,
        ):
            _RENEWAL_ATTEMPTS[key] = now
            logger.info(
                "upstream_proof_renewal user_id=%s connection_id=%s tenants=%d",
                user_id,
                connection_id,
                len(tenant_ids),
                extra={"user_id": user_id, "connection_id": str(connection_id)},
            )


def _admission_from(check: FreshnessCheck, results, graced=frozenset()) -> UpstreamAdmission:
    if check.unbound:
        # A legacy membership with no bound credential can never be rechecked, and
        # proofs are deliberately not backfilled; only reconnecting repairs it.
        return UpstreamAdmission(admitted=False, reason=CREDENTIAL_MISSING)
    if results is None:
        return ADMITTED
    reason = most_severe(filter(None, (denial_reason(result) for result in results)))
    return UpstreamAdmission(admitted=reason is None, rechecked=True, reason=reason, graced=graced)


def _on_server_loop_thread() -> bool:
    """Whether async_to_sync here runs on the server's long-lived loop.

    It does in a sync view under ASGI (asgiref records that loop for the threads it
    runs sync code in). Elsewhere (runserver, shell, management commands) it runs a
    throwaway loop that cancels the background recheck grace depends on.
    """
    loop = getattr(SyncToAsync.threadlocal, "main_event_loop", None)
    return (
        getattr(SyncToAsync.threadlocal, "main_event_loop_pid", None) == os.getpid()
        and loop is not None
        and loop.is_running()
    )


def admit_upstream(user_id, tenant_ids, *, budget: VerificationBudget) -> UpstreamAdmission:
    """Sync twin of :func:`aadmit_upstream` for DRF and other thread-bound callers.

    Raises ``RuntimeError`` (from ``async_to_sync``) if called on a thread that is
    already running an event loop; async code must use the async twin.
    """
    check = check_freshness(user_id, tenant_ids)
    recheck = bool(check.stale) and not check.unbound
    if recheck and transaction.get_connection().in_atomic_block:
        # The verification's ORM work would run as savepoints of the caller's
        # transaction: its row locks held across provider I/O, and the lease and
        # proofs invisible to other verifiers until commit. Recheck outside.
        return UpstreamAdmission(admitted=False, reason=VERIFICATION_IN_PROGRESS)
    # Grace and renewal both leave a task behind, which only the server loop keeps.
    # Past the atomic-block guard only when nothing is stale: that branch must stay
    # free of ORM awaits, and the renewal's own ORM work runs on a detached pool.
    on_server_loop = _on_server_loop_thread()
    if not recheck and not (on_server_loop and _renewal_due(check, budget)):
        return _admission_from(check, None)
    # Thread-bound caller (DRF); the async twin serves async callers.
    return async_to_sync(_aadmit_checked)(
        user_id, check, budget, allow_grace=on_server_loop, renew=on_server_loop
    )


async def _aadmit_checked(
    user_id, check: FreshnessCheck, budget: VerificationBudget, *, allow_grace=True, renew=True
) -> UpstreamAdmission:
    results = None
    graced = frozenset()
    if check.stale and not check.unbound:
        results, graced = await _averify_stale_with_grace(
            user_id, check.stale, budget, allow_grace=allow_grace
        )
    admission = _admission_from(check, results, graced)
    # After the decision: a request denied on another connection renews nothing.
    if renew and admission.admitted and _renewal_due(check, budget):
        _schedule_renewals(user_id, check)
    return admission


async def aadmit_upstream(user_id, tenant_ids, *, budget: VerificationBudget) -> UpstreamAdmission:
    return await _aadmit_checked(user_id, await acheck_freshness(user_id, tenant_ids), budget)


async def averify_membership_history(
    user_id, tenant_ids, *, budget: VerificationBudget
) -> str | None:
    """Recheck every connection the user has held these tenants through, live or archived.

    Recovery must include archived tombstones: a confirmed revocation archives the
    membership, and only a successful verification of the same connection may restore
    it. Returns the most severe denial reason, or ``None`` when every check verified.
    """
    rows = [
        row
        async for row in TenantMembership.all_objects.filter(
            user_id=user_id, tenant_id__in=list(tenant_ids), connection__isnull=False
        ).values_list("tenant_id", "connection_id", "archived_at")
    ]
    grouped, _unbound = _group_by_connection((tenant, conn) for tenant, conn, _at in rows)
    if not grouped:
        return CREDENTIAL_MISSING
    live_connections = {conn for _tenant, conn, archived_at in rows if archived_at is None}
    stale = {connection_id: frozenset(ids) for connection_id, ids in grouped.items()}
    results = dict(zip(stale, await _averify_stale(user_id, stale, budget), strict=True))
    # A dead tombstone connection must not mask a transient failure on the credential
    # that still backs live access, or the user loses the retryable state.
    live = [conn for conn in results if conn in live_connections]
    if live:
        return most_severe(filter(None, (denial_reason(results[conn]) for conn in live)))
    # Nothing live: any one connection can restore any-of access, so a retryable
    # outcome anywhere must not be masked by a terminal one elsewhere.
    reasons = [reason for reason in map(denial_reason, results.values()) if reason]
    if len(reasons) < len(results):
        return None  # a connection verified; the caller re-reads what it restored
    retryable = [reason for reason in reasons if reason in RETRYABLE_REASONS]
    return most_severe(retryable or reasons)


def final_denial_reason(admission: UpstreamAdmission, final: FreshnessCheck) -> str:
    """Reason to report when post-recheck local state still lacks a fresh proof."""
    if final.unbound:
        return CREDENTIAL_MISSING
    # Upstream answered yet a proof is still missing: a concurrent rotation,
    # denial or composition change landed in between, so another attempt can pass.
    return admission.reason or VERIFICATION_IN_PROGRESS
