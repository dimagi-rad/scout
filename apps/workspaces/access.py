"""The single source of truth for "can this user access this workspace?".

Effective access = the user is a ``WorkspaceMembership`` of the workspace AND
(the workspace has no tenants OR the user has at least one *live* — non-archived —
``TenantMembership`` for one of the workspace's tenants). This unifies the
add-member rule with every runtime gate: a member who loses all upstream tenant
access loses the workspace (manage *and* query), and regains it automatically if
access is restored upstream — no human-in-Scout reinstatement.

Because ``TenantMembership.objects`` is live-only (archived rows are tombstones for
revoked access), the tenant check here naturally ignores revoked access. Every
workspace-scoped view/tool MUST resolve access through this module; a CI fitness
test (tests/test_authorizer_is_sole_gate.py) fails the build on a bypass.

Denial is not one thing. A user who was never a member (or whose workspace is gone)
gets a generic denial; a member who merely lost all live tenant access gets a
distinct, actionable one naming the lost project(s) — so callers can explain "your
upstream access was removed" instead of a dead, unexplained 403. The
``(workspace, membership)`` tuple API is preserved; ``*_ex`` variants expose the
reason, and ``access_denied_body`` builds the response payload from it.

With ``UPSTREAM_ACCESS_FRESHNESS_ENFORCED`` on, a locally granted decision must also
pass upstream-freshness admission (``services/access_freshness.py``): stale proofs
are rechecked before protected data is released, and a check that cannot complete
is a retryable denial rather than a lost membership.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantMembership
from apps.workspaces.models import WorkspaceMembership, WorkspaceRole
from apps.workspaces.services.access_freshness import (
    CREDENTIAL_EXPIRED,
    CREDENTIAL_MISSING,
    RETRYABLE_REASONS,
    UPSTREAM_ACCESS_LOST,
    VERIFICATION_IN_PROGRESS,
    VERIFICATION_UNAVAILABLE,
    UpstreamAdmission,
    VerificationBudget,
    aadmit_upstream,
    acheck_freshness,
    admit_upstream,
    averify_membership_history,
    check_freshness,
    final_denial_reason,
    freshness_enforced,
)
from apps.workspaces.services.failure_guidance import CREDENTIAL_GUIDANCE

NOT_MEMBER = "not_member"
TENANT_ACCESS_LOST = "tenant_access_lost"
INSUFFICIENT_ROLE = "insufficient_role"

_ROLE_RANK = {
    WorkspaceRole.READ: 0,
    WorkspaceRole.READ_WRITE: 1,
    WorkspaceRole.MANAGE: 2,
}

_GENERIC_DENIED = "Workspace not found or access denied."
TOOL_READ_DENIED_MESSAGE = "Workspace access required for this operation."
TOOL_WRITE_DENIED_MESSAGE = "Read-write or manage role required for this operation."


@dataclass(frozen=True)
class WorkspaceAccess:
    """Outcome of an access decision.

    ``workspace``/``membership`` are set iff access is granted. On denial they are
    ``None`` and ``denied_reason`` is one of ``NOT_MEMBER`` / ``TENANT_ACCESS_LOST`` /
    ``INSUFFICIENT_ROLE`` or an upstream-freshness reason (``FRESHNESS_DENIAL_REASONS``);
    ``lost_tenant_names`` names the workspace's tenants the user no longer shares.
    """

    workspace: object | None = None
    membership: object | None = None
    denied_reason: str | None = None
    lost_tenant_names: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        return self.workspace is not None

    @property
    def retryable(self) -> bool:
        return self.denied_reason in RETRYABLE_REASONS


def _freshness_denied(reason: str | None) -> WorkspaceAccess:
    return WorkspaceAccess(denied_reason=reason or VERIFICATION_UNAVAILABLE)


def _attribute_observed_denial(result: WorkspaceAccess, admission) -> WorkspaceAccess:
    """Name the upstream cause when this very recheck is what archived the coverage.

    A generic lost-coverage denial reads as "not connected"; a revocation or dead
    sign-in just observed upstream needs its own remedy (ask a provider admin, or
    reconnect), so keep the lost tenant names and report the observed reason.
    """
    if result.denied_reason == TENANT_ACCESS_LOST and admission.reason in (
        UPSTREAM_ACCESS_LOST,
        CREDENTIAL_EXPIRED,
    ):
        return WorkspaceAccess(
            denied_reason=admission.reason, lost_tenant_names=result.lost_tenant_names
        )
    return result


CONNECTED_ACCOUNTS_PATH = "/settings/connections"
RETRY_COOLDOWN_SECONDS = 10
_RETRY_PENDING = "pending"
_REPLAYABLE_REASONS = frozenset(
    {
        TENANT_ACCESS_LOST,
        CREDENTIAL_MISSING,
        CREDENTIAL_EXPIRED,
        UPSTREAM_ACCESS_LOST,
        VERIFICATION_UNAVAILABLE,
        VERIFICATION_IN_PROGRESS,
    }
)

_FRESHNESS_MESSAGES = {
    CREDENTIAL_MISSING: (
        "Scout has no usable connection for one of this workspace's sources. "
        "Reconnect it under Connected Accounts."
    ),
    CREDENTIAL_EXPIRED: (
        "Your sign-in for one of this workspace's sources has expired. "
        "Reconnect it under Connected Accounts."
    ),
    UPSTREAM_ACCESS_LOST: (
        "Your access to one of this workspace's sources was removed upstream. "
        + CREDENTIAL_GUIDANCE[ErrorCode.AUTH_ACCESS_DENIED]
    ),
    VERIFICATION_UNAVAILABLE: (
        "We couldn't verify your access to this workspace right now. Please retry shortly."
    ),
    VERIFICATION_IN_PROGRESS: (
        "Your access to this workspace is being verified. Please retry in a moment."
    ),
}


def access_denied_body(result: WorkspaceAccess) -> dict:
    """Build the 403 response body for a denied access result.

    Preserves the generic ``{"error": ...}`` shape for backward compatibility and,
    for lost upstream access, adds ``reason`` + ``lost_tenants`` and an actionable
    message the frontend can surface verbatim.
    """
    if result.denied_reason == TENANT_ACCESS_LOST and result.lost_tenant_names:
        projects = ", ".join(result.lost_tenant_names)
        return {
            "error": (
                f"You no longer have access to {projects}: "
                + CREDENTIAL_GUIDANCE[ErrorCode.WORKSPACE_TENANT_UNREACHABLE]
            ),
            "reason": TENANT_ACCESS_LOST,
            "lost_tenants": list(result.lost_tenant_names),
        }
    if result.denied_reason in _FRESHNESS_MESSAGES:
        body = {
            "error": _FRESHNESS_MESSAGES[result.denied_reason],
            "reason": result.denied_reason,
            "retryable": result.retryable,
            "recovery_url": CONNECTED_ACCOUNTS_PATH,
        }
        if result.lost_tenant_names:
            body["lost_tenants"] = list(result.lost_tenant_names)
        return body
    return {"error": _GENERIC_DENIED}


def _live_tenant_ids(workspace) -> list:
    return list(workspace.workspace_tenants.values_list("tenant_id", flat=True))


async def _alive_tenant_ids(workspace) -> list:
    return [
        tenant_id
        async for tenant_id in workspace.workspace_tenants.values_list("tenant_id", flat=True)
    ]


def _tenant_rows(workspace) -> list[tuple]:
    return list(workspace.workspace_tenants.values_list("tenant_id", "tenant__canonical_name"))


async def _atenant_rows(workspace) -> list[tuple]:
    return [
        row
        async for row in workspace.workspace_tenants.values_list(
            "tenant_id", "tenant__canonical_name"
        )
    ]


def _lost_names(rows) -> tuple[str, ...]:
    return tuple(sorted({name for _tid, name in rows if name}))


def _shares_live_tenant(user, tenant_ids) -> bool:
    # Zero-tenant workspace: nothing to gate on, WorkspaceMembership suffices.
    if not tenant_ids:
        return True
    return TenantMembership.objects.filter(user=user, tenant_id__in=tenant_ids).exists()


async def _ashares_live_tenant(user, tenant_ids) -> bool:
    if not tenant_ids:
        return True
    return await TenantMembership.objects.filter(user=user, tenant_id__in=tenant_ids).aexists()


def covers_live_tenants(user, tenant_ids) -> bool:
    """Whether the user has a live ``TenantMembership`` for EVERY id in ``tenant_ids``.

    **Not the gate.** ``_shares_live_tenant`` above is still what decides access,
    and it is still any-of; flipping it to covering-all is #380 and lands after
    this. This predicate exists so the covering rule can be evaluated against
    real membership data now.

    Its purpose is to make the #156 → #380 dependency checkable: covering-all was
    blocked because a user holding two OCS teams could only ever prove one, so the
    rule would have denied a workspace spanning both with no way to self-remediate.
    With multi-token OAuth that user holds a live membership for each tenant, and
    this returns True — see
    ``tests/test_ocs_multi_team_oauth.py::test_two_team_user_covers_an_all_of_workspace``.
    """
    if not tenant_ids:
        return True
    wanted = set(tenant_ids)
    covered = set(
        TenantMembership.objects.filter(user=user, tenant_id__in=wanted).values_list(
            "tenant_id", flat=True
        )
    )
    return wanted <= covered


async def acovers_live_tenants(user, tenant_ids) -> bool:
    """Async twin of ``covers_live_tenants``. Not the gate — see that docstring."""
    if not tenant_ids:
        return True
    wanted = set(tenant_ids)
    covered = {
        tid
        async for tid in TenantMembership.objects.filter(
            user=user, tenant_id__in=wanted
        ).values_list("tenant_id", flat=True)
    }
    return wanted <= covered


def _role_satisfies(role: str, minimum_role: str) -> bool:
    role_rank = _ROLE_RANK.get(role)
    minimum_rank = _ROLE_RANK.get(minimum_role)
    return role_rank is not None and minimum_rank is not None and role_rank >= minimum_rank


def _resolve_local_access_ex(user, workspace_id, *, minimum_role: str) -> WorkspaceAccess:
    """Membership, tenant coverage and role, from local state only."""
    try:
        wm = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return WorkspaceAccess(denied_reason=NOT_MEMBER)
    rows = _tenant_rows(wm.workspace)
    if not _shares_live_tenant(user, [tid for tid, _name in rows]):
        return WorkspaceAccess(
            denied_reason=TENANT_ACCESS_LOST, lost_tenant_names=_lost_names(rows)
        )
    if not _role_satisfies(wm.role, minimum_role):
        return WorkspaceAccess(denied_reason=INSUFFICIENT_ROLE)
    return WorkspaceAccess(workspace=wm.workspace, membership=wm)


async def _aresolve_local_access_ex(user, workspace_id, *, minimum_role: str) -> WorkspaceAccess:
    """Async twin of ``_resolve_local_access_ex``."""
    try:
        wm = await WorkspaceMembership.objects.select_related("workspace").aget(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return WorkspaceAccess(denied_reason=NOT_MEMBER)
    rows = await _atenant_rows(wm.workspace)
    if not await _ashares_live_tenant(user, [tid for tid, _name in rows]):
        return WorkspaceAccess(
            denied_reason=TENANT_ACCESS_LOST, lost_tenant_names=_lost_names(rows)
        )
    if not _role_satisfies(wm.role, minimum_role):
        return WorkspaceAccess(denied_reason=INSUFFICIENT_ROLE)
    return WorkspaceAccess(workspace=wm.workspace, membership=wm)


def resolve_workspace_access_ex(
    user,
    workspace_id,
    *,
    minimum_role: str = WorkspaceRole.READ,
    verification: VerificationBudget | None = VerificationBudget.INTERACTIVE,
) -> WorkspaceAccess:
    """Resolve access, exposing the denial reason (see ``WorkspaceAccess``).

    ``verification`` selects the upstream-freshness budget for protected data;
    ``None`` is the recovery-metadata mode, which needs membership but must stay
    reachable while upstream verification is failing.
    """
    result = _resolve_local_access_ex(user, workspace_id, minimum_role=minimum_role)
    if verification is None or not result.granted or not freshness_enforced():
        return result
    admission = admit_upstream(user.pk, _live_tenant_ids(result.workspace), budget=verification)
    if not admission.rechecked:
        return result if admission.admitted else _freshness_denied(admission.reason)
    result = _resolve_local_access_ex(user, workspace_id, minimum_role=minimum_role)
    if not result.granted:
        return _attribute_observed_denial(result, admission)
    final = check_freshness(user.pk, _live_tenant_ids(result.workspace))
    return result if final.fresh else _freshness_denied(final_denial_reason(admission, final))


async def aresolve_workspace_access_ex(
    user,
    workspace_id,
    *,
    minimum_role: str = WorkspaceRole.READ,
    verification: VerificationBudget | None = VerificationBudget.INTERACTIVE,
) -> WorkspaceAccess:
    """Async twin of ``resolve_workspace_access_ex``."""
    result = await _aresolve_local_access_ex(user, workspace_id, minimum_role=minimum_role)
    if verification is None or not result.granted or not freshness_enforced():
        return result
    admission = await aadmit_upstream(
        user.pk, await _alive_tenant_ids(result.workspace), budget=verification
    )
    if not admission.rechecked:
        return result if admission.admitted else _freshness_denied(admission.reason)
    result = await _aresolve_local_access_ex(user, workspace_id, minimum_role=minimum_role)
    if not result.granted:
        return _attribute_observed_denial(result, admission)
    final = await acheck_freshness(user.pk, await _alive_tenant_ids(result.workspace))
    return result if final.fresh else _freshness_denied(final_denial_reason(admission, final))


async def aretry_workspace_verification(user, workspace_id) -> WorkspaceAccess:
    """Explicit member-initiated recheck, reachable while protected access is denied.

    Only membership is required up front — a member whose tenant was archived by a
    revocation still qualifies — and only the caller's own connections are checked.
    The final decision is read back from the database rather than inferred from the
    provider answer, so a concurrent change cannot be reported as restored access.
    """
    local = await _aresolve_local_access_ex(user, workspace_id, minimum_role=WorkspaceRole.READ)
    if local.denied_reason == NOT_MEMBER:
        return local
    workspace = local.workspace
    if workspace is None:
        try:
            membership = await WorkspaceMembership.objects.select_related("workspace").aget(
                workspace_id=workspace_id, user=user
            )
        except WorkspaceMembership.DoesNotExist:
            return WorkspaceAccess(denied_reason=NOT_MEMBER)
        workspace = membership.workspace
    tenant_ids = await _alive_tenant_ids(workspace)
    if not tenant_ids or not freshness_enforced():
        return local
    # Keyed on the user: the protected resource is their connections, which any of
    # their workspaces could otherwise re-trigger. A tombstoned history can never
    # short-circuit as fresh, so without this every retry is a provider round-trip.
    cooldown_key = f"access-verify-retry:{user.pk}"
    if not await cache.aadd(cooldown_key, _RETRY_PENDING, RETRY_COOLDOWN_SECONDS):
        if local.granted and (await acheck_freshness(user.pk, tenant_ids)).fresh:
            return local
        replayed = await cache.aget(cooldown_key)
        # The lease is per user but a concluded reason belongs to one workspace.
        if isinstance(replayed, dict) and replayed.get("workspace") == str(workspace_id):
            return WorkspaceAccess(
                denied_reason=replayed["reason"],
                lost_tenant_names=tuple(replayed.get("lost", ())),
            )
        return _freshness_denied(VERIFICATION_IN_PROGRESS)
    retry_reason = await averify_membership_history(
        user.pk, tenant_ids, budget=VerificationBudget.INTERACTIVE
    )
    admission = UpstreamAdmission(admitted=False, rechecked=True, reason=retry_reason)
    result = await _aresolve_local_access_ex(user, workspace_id, minimum_role=WorkspaceRole.READ)
    if not result.granted:
        if retry_reason in RETRYABLE_REASONS:
            result = _freshness_denied(retry_reason)
        else:
            result = _attribute_observed_denial(result, admission)
    else:
        final = await acheck_freshness(user.pk, await _alive_tenant_ids(result.workspace))
        if not final.fresh:
            result = _freshness_denied(final_denial_reason(admission, final))
    # Re-arm after the check: a slow provider can outlast the first window.
    # Kept even on success: a tombstoned history can never short-circuit as fresh, so
    # a granted retry still cost a provider call and must stay throttled.
    concluded = _RETRY_PENDING
    if not result.granted and result.denied_reason in _REPLAYABLE_REASONS:
        concluded = {
            "workspace": str(workspace_id),
            "reason": result.denied_reason,
            "lost": list(result.lost_tenant_names),
        }
    await cache.aset(cooldown_key, concluded, RETRY_COOLDOWN_SECONDS)
    return result


def resolve_workspace_access(user, workspace_id, *, minimum_role: str = WorkspaceRole.READ):
    """Return ``(workspace, WorkspaceMembership)`` if the user has access, else ``(None, None)``."""
    result = resolve_workspace_access_ex(user, workspace_id, minimum_role=minimum_role)
    return result.workspace, result.membership


async def aresolve_workspace_access(user, workspace_id, *, minimum_role: str = WorkspaceRole.READ):
    """Async: return ``(workspace, WorkspaceMembership)`` on access, else ``(None, None)``."""
    result = await aresolve_workspace_access_ex(user, workspace_id, minimum_role=minimum_role)
    return result.workspace, result.membership


def workspace_write_allowed(user, workspace_id) -> bool:
    """Return whether an actor currently has shared-write authority."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return resolve_workspace_access_ex(
        user, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
    ).granted


async def aworkspace_read_allowed(user, workspace_id) -> bool:
    """Return whether an actor currently has workspace read authority."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return (await aresolve_workspace_access_ex(user, workspace_id)).granted


async def aworkspace_write_allowed(user, workspace_id) -> bool:
    """Async twin of ``workspace_write_allowed``."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return (
        await aresolve_workspace_access_ex(
            user, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
    ).granted


def tool_write_denied() -> dict:
    """Structured denial returned by local LangChain mutation tools."""
    return {
        "status": "denied",
        "message": TOOL_WRITE_DENIED_MESSAGE,
        "error": {"code": "FORBIDDEN", "message": TOOL_WRITE_DENIED_MESSAGE},
    }


def tool_read_denied() -> dict:
    """Structured denial returned by local LangChain read tools."""
    return {
        "status": "denied",
        "message": TOOL_READ_DENIED_MESSAGE,
        "error": {"code": "FORBIDDEN", "message": TOOL_READ_DENIED_MESSAGE},
    }
