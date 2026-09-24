"""Upstream-freshness admission for protected workspace access.

``apps.workspaces.access`` decides from local state whether a member may use a
workspace. This layer adds the confirmed freshness policy
(docs/meeting-readiness-2026-09-17/FRESHNESS-CONTRACT.md): every live tenant
membership the decision relies on must hold an upstream proof younger than five
minutes. A stale proof is rechecked through the connection-elected verification
service, so concurrent callers share one provider round-trip.

Outcomes: fresh → admit with no provider call; authoritative revocation → deny,
with the membership archival already persisted by the verification publisher;
provider unavailable or indeterminate → a retryable denial that leaves every
membership in place.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import transaction

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantMembership
from apps.users.services.access_verification import aproofs_are_fresh, proofs_are_fresh
from apps.users.services.access_verification_providers import PROVIDER_BUDGET_SECONDS
from apps.users.services.access_verification_service import verify_connection_access
from apps.users.services.access_verification_types import (
    AccessVerificationResult,
    AccessVerificationStatus,
)

logger = logging.getLogger(__name__)


class VerificationBudget(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


# An interactive caller is a person waiting on a response; a worker can afford the
# provider adapter's full budget. Either way the deadline is end-to-end across every
# connection being rechecked, never multiplied per connection (FOLLOW-UPS #7).
BUDGET_SECONDS = {
    VerificationBudget.INTERACTIVE: 10.0,
    VerificationBudget.BACKGROUND: PROVIDER_BUDGET_SECONDS,
}

CREDENTIAL_MISSING = "credential_missing"
CREDENTIAL_EXPIRED = "credential_expired"
UPSTREAM_ACCESS_LOST = "upstream_access_lost"
VERIFICATION_UNAVAILABLE = "verification_unavailable"
VERIFICATION_IN_PROGRESS = "verification_in_progress"

FRESHNESS_DENIAL_REASONS = (
    CREDENTIAL_MISSING,
    CREDENTIAL_EXPIRED,
    UPSTREAM_ACCESS_LOST,
    VERIFICATION_UNAVAILABLE,
    VERIFICATION_IN_PROGRESS,
)
RETRYABLE_REASONS = frozenset({VERIFICATION_UNAVAILABLE, VERIFICATION_IN_PROGRESS})

# Registry codes for surfaces that report per-source failures (worker summaries,
# MCP envelopes); reuse the codes whose remedies already exist.
FRESHNESS_ERROR_CODES: dict[str, ErrorCode] = {
    CREDENTIAL_MISSING: ErrorCode.AUTH_CREDENTIAL_MISSING,
    CREDENTIAL_EXPIRED: ErrorCode.AUTH_TOKEN_EXPIRED,
    UPSTREAM_ACCESS_LOST: ErrorCode.AUTH_ACCESS_DENIED,
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


def check_freshness(user_id, tenant_ids) -> FreshnessCheck:
    grouped, unbound = _group_by_connection(_live_bindings_queryset(user_id, list(tenant_ids)))
    stale = {
        connection_id: frozenset(ids)
        for connection_id, ids in grouped.items()
        if not proofs_are_fresh(user_id, connection_id, ids)
    }
    return FreshnessCheck(stale=stale, unbound=frozenset(unbound))


async def acheck_freshness(user_id, tenant_ids) -> FreshnessCheck:
    rows = [row async for row in _live_bindings_queryset(user_id, list(tenant_ids))]
    grouped, unbound = _group_by_connection(rows)
    stale = {}
    for connection_id, ids in grouped.items():
        if not await aproofs_are_fresh(user_id, connection_id, ids):
            stale[connection_id] = frozenset(ids)
    return FreshnessCheck(stale=stale, unbound=frozenset(unbound))


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
    return VERIFICATION_UNAVAILABLE


def most_severe(reasons) -> str | None:
    present = set(reasons)
    return next((reason for reason in FRESHNESS_DENIAL_REASONS if reason in present), None)


async def _averify_stale(user_id, stale: dict, budget: VerificationBudget) -> list:
    deadline = time.monotonic() + BUDGET_SECONDS[budget]
    results = await asyncio.gather(
        *(
            verify_connection_access(user_id, connection_id, tenant_ids, deadline=deadline)
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
    return [
        AccessVerificationResult(AccessVerificationStatus.UNAVAILABLE, VERIFICATION_UNAVAILABLE)
        if isinstance(result, Exception)
        else result
        for result in results
    ]


def _admission_from(check: FreshnessCheck, results) -> UpstreamAdmission:
    if check.unbound:
        # A legacy membership with no bound credential can never be rechecked, and
        # proofs are deliberately not backfilled; only reconnecting repairs it.
        return UpstreamAdmission(admitted=False, reason=CREDENTIAL_MISSING)
    if results is None:
        return ADMITTED
    reason = most_severe(filter(None, (denial_reason(result) for result in results)))
    return UpstreamAdmission(admitted=reason is None, rechecked=True, reason=reason)


def admit_upstream(user_id, tenant_ids, *, budget: VerificationBudget) -> UpstreamAdmission:
    """Sync twin of :func:`aadmit_upstream` for DRF and other thread-bound callers.

    Raises ``RuntimeError`` (from ``async_to_sync``) if called on a thread that is
    already running an event loop; async code must use the async twin.
    """
    check = check_freshness(user_id, tenant_ids)
    results = None
    if check.stale and not check.unbound:
        if transaction.get_connection().in_atomic_block:
            # The verification's ORM work would run as savepoints of the caller's
            # transaction: its row locks held across provider I/O, and the lease and
            # proofs invisible to other verifiers until commit. Recheck outside.
            return UpstreamAdmission(admitted=False, reason=VERIFICATION_IN_PROGRESS)
        results = async_to_sync(_averify_stale)(user_id, check.stale, budget)
    return _admission_from(check, results)


async def aadmit_upstream(user_id, tenant_ids, *, budget: VerificationBudget) -> UpstreamAdmission:
    check = await acheck_freshness(user_id, tenant_ids)
    results = None
    if check.stale and not check.unbound:
        results = await _averify_stale(user_id, check.stale, budget)
    return _admission_from(check, results)


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
