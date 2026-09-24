"""The single source of truth for "can this user access this workspace?".

Effective access = the user is a ``WorkspaceMembership`` of the workspace AND
(the workspace has no tenants OR the user can use EVERY one of its tenants with
their own credential). All-of, not any-of (#380): a workspace exposes one merged
view over all of its tenants, so a member covering only some of them would read
the rest. A member who loses one tenant upstream therefore loses the workspace
(manage *and* query) and regains it automatically once coverage is restored — no
human-in-Scout reinstatement, and the membership itself is never deleted.

"Can use" is local credential readiness from
``apps.workspaces.services.credential_coverage`` rather than bare
``TenantMembership`` presence: a live row whose credential cannot be resolved
(legacy OCS rows with no team, a connection bound to another team, a sign-in that
can no longer refresh) would otherwise pass the gate while every load of that
tenant fails closed (#380, 2026-09-10 update). It is not upstream liveness;
freshness is a separate check.

Every workspace-scoped view/tool MUST resolve access through this module; a CI
fitness test (tests/test_authorizer_is_sole_gate.py) fails the build on a bypass.

Denial is not one thing. A user who was never a member (or whose workspace is gone)
gets a generic denial; a member who lacks one or more tenants gets a distinct,
actionable one naming each missing source and its remedy — so callers can say
"connect team Y" instead of a dead, unexplained 403. The
``(workspace, membership)`` tuple API is preserved; ``*_ex`` variants expose the
reason, and ``access_denied_body`` builds the response payload from it.

With ``UPSTREAM_ACCESS_FRESHNESS_ENFORCED`` on, a locally granted decision (the
coverage check above) must also pass upstream-freshness admission
(``services/access_freshness.py``): stale proofs are rechecked before protected data
is released, and a check that cannot complete is a retryable denial rather than a
lost membership. The two switches are independent.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from apps.users.models import PROVIDER_CHOICES, TenantMembership
from apps.workspaces.models import WorkspaceMembership, WorkspaceRole
from apps.workspaces.services.access_freshness import (
    CREDENTIAL_EXPIRED,
    CREDENTIAL_MISSING,
    RETRYABLE_REASONS,
    UPSTREAM_ACCESS_LOST,
    VERIFICATION_IN_PROGRESS,
    VERIFICATION_UNAVAILABLE,
    VerificationBudget,
    aadmit_upstream,
    acheck_freshness,
    admit_upstream,
    check_freshness,
    final_denial_reason,
    freshness_enforced,
)
from apps.workspaces.services.credential_coverage import (
    CoverageRecovery,
    MissingTenant,
    amember_coverage_gaps,
    member_coverage_gaps,
)

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

_PROVIDER_LABELS = dict(PROVIDER_CHOICES)


@dataclass(frozen=True)
class WorkspaceAccess:
    """Outcome of an access decision.

    ``workspace``/``membership`` are set iff access is granted. On denial they are
    ``None`` and ``denied_reason`` is one of ``NOT_MEMBER`` / ``TENANT_ACCESS_LOST`` /
    ``INSUFFICIENT_ROLE`` or an upstream-freshness reason; for ``TENANT_ACCESS_LOST``,
    ``missing_tenants`` lists each workspace tenant the member cannot use, with its
    remedy.
    """

    workspace: object | None = None
    membership: object | None = None
    denied_reason: str | None = None
    missing_tenants: tuple[MissingTenant, ...] = ()

    @property
    def granted(self) -> bool:
        return self.workspace is not None

    @property
    def lost_tenant_names(self) -> tuple[str, ...]:
        return tuple(sorted({t.tenant_name for t in self.missing_tenants if t.tenant_name}))

    @property
    def retryable(self) -> bool:
        return self.denied_reason in RETRYABLE_REASONS


def _freshness_denied(reason: str | None) -> WorkspaceAccess:
    return WorkspaceAccess(denied_reason=reason or VERIFICATION_UNAVAILABLE)


def _attribute_observed_denial(result: WorkspaceAccess, admission) -> WorkspaceAccess:
    """Name the upstream cause when this very recheck is what archived the coverage.

    A generic lost-coverage denial reads as "not connected"; a revocation or dead
    sign-in just observed upstream needs its own remedy (ask a provider admin, or
    reconnect), so keep the missing tenants and report the observed reason.
    """
    if result.denied_reason == TENANT_ACCESS_LOST and admission.reason in (
        UPSTREAM_ACCESS_LOST,
        CREDENTIAL_EXPIRED,
    ):
        return WorkspaceAccess(
            denied_reason=admission.reason, missing_tenants=result.missing_tenants
        )
    return result


CONNECTED_ACCOUNTS_PATH = "/settings/connections"

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
        "Reconnect or ask an admin to restore it."
    ),
    VERIFICATION_UNAVAILABLE: (
        "We couldn't verify your access to this workspace right now. Please retry shortly."
    ),
    VERIFICATION_IN_PROGRESS: (
        "Your access to this workspace is being verified. Please retry in a moment."
    ),
}


def remedy_text(missing: MissingTenant) -> str:
    """One clause telling the member how to regain ``missing``."""
    product = _PROVIDER_LABELS.get(missing.provider, "its provider")
    team = missing.team_name or missing.team_slug
    if missing.recovery == CoverageRecovery.ACCESS_REMOVED:
        # The tombstone can't tell a disconnect from upstream removal, so name both.
        return (
            f"your access through {product} ended: reconnect it in Connected Accounts, "
            f"or if it was removed in {product}, ask an admin there to restore it"
        )
    if missing.recovery == CoverageRecovery.CONNECT_TEAM and team:
        return f"connect {product} team '{team}' in Connected Accounts"
    if missing.recovery == CoverageRecovery.LEGACY_TEAM_UNKNOWN:
        return f"reconnect {product}, choosing the team that owns it, in Connected Accounts"
    if missing.recovery == CoverageRecovery.CONNECT_TEAM:
        return f"connect the {product} team that owns it in Connected Accounts"
    if missing.recovery == CoverageRecovery.RECONNECT:
        return f"reconnect {product} in Connected Accounts"
    return f"connect an account on {product} that has access to it in Connected Accounts"


def missing_tenants_payload(missing) -> list[dict]:
    """Serialize missing tenants, name-ordered, with the remedy for each."""
    ordered = sorted(missing, key=lambda t: (t.tenant_name, t.tenant_id))
    return [t.as_dict() | {"remedy": remedy_text(t)} for t in ordered]


def access_denied_body(result: WorkspaceAccess) -> dict:
    """Build the 403 response body for a denied access result.

    Preserves the generic ``{"error": ...}`` shape for backward compatibility and,
    for a member missing tenants, adds ``reason``, ``lost_tenants`` and the
    structured ``missing_tenants`` plus an actionable message the frontend can
    surface verbatim.

    ``TENANT_ACCESS_LOST`` covers "never had" as well as "lost": for a member the
    consequence is identical (content cleared, recovery via Connected Accounts),
    and one member can be missing a tenant each way at once, so the distinction
    lives per tenant in ``recovery`` rather than in a second reason code.
    """
    if result.denied_reason == TENANT_ACCESS_LOST and result.missing_tenants:
        payload = missing_tenants_payload(result.missing_tenants)
        needed = "; ".join(
            f"'{t['tenant_name'] or _PROVIDER_LABELS.get(t['provider'], 'a source')}': "
            f"{t['remedy']}"
            for t in payload
        )
        rule = (
            "This workspace requires access to every one of its data sources. Still needed"
            if all_of_access_enforced()
            else "You need access to at least one of this workspace's data sources. Options"
        )
        return {
            "error": f"{rule} — {needed}. Access returns automatically once fixed.",
            "reason": TENANT_ACCESS_LOST,
            "lost_tenants": list(result.lost_tenant_names),
            "missing_tenants": payload,
        }
    if result.denied_reason in _FRESHNESS_MESSAGES:
        body = {
            "error": _FRESHNESS_MESSAGES[result.denied_reason],
            "reason": result.denied_reason,
            "retryable": result.retryable,
            "recovery_url": CONNECTED_ACCOUNTS_PATH,
        }
        if result.missing_tenants:
            body["lost_tenants"] = list(result.lost_tenant_names)
            body["missing_tenants"] = missing_tenants_payload(result.missing_tenants)
        return body
    return {"error": _GENERIC_DENIED}


def _live_tenant_ids(workspace) -> list:
    return list(workspace.workspace_tenants.values_list("tenant_id", flat=True))


async def _alive_tenant_ids(workspace) -> list:
    return [
        tenant_id
        async for tenant_id in workspace.workspace_tenants.values_list("tenant_id", flat=True)
    ]


def _shares_live_tenant(user, tenant_ids) -> bool:
    # Pre-#380 any-of rule: the read gate's fallback while the rollout switch is off.
    # Member admission still uses it until #561 moves admission to all-of too.
    if not tenant_ids:
        return True
    return TenantMembership.objects.filter(user=user, tenant_id__in=tenant_ids).exists()


async def _ashares_live_tenant(user, tenant_ids) -> bool:
    if not tenant_ids:
        return True
    return await TenantMembership.objects.filter(user=user, tenant_id__in=tenant_ids).aexists()


def all_of_access_enforced() -> bool:
    """Rollout switch for the read gate; see ``WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT``."""
    return settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT


def missing_workspace_tenants(user, tenants) -> tuple[MissingTenant, ...]:
    """The tenants among ``tenants`` that keep ``user`` out; empty means access.

    With the rollout switch off, one live membership still satisfies the
    workspace and the gaps are computed only to explain a denial.
    """
    tenants = list(tenants)
    if not tenants:
        return ()
    if not all_of_access_enforced() and _shares_live_tenant(user, [t.pk for t in tenants]):
        return ()
    return tuple(member_coverage_gaps(user.pk, tenants).values())


async def amissing_workspace_tenants(user, tenants) -> tuple[MissingTenant, ...]:
    """Async twin of :func:`missing_workspace_tenants`."""
    tenants = list(tenants)
    if not tenants:
        return ()
    if not all_of_access_enforced() and await _ashares_live_tenant(user, [t.pk for t in tenants]):
        return ()
    return tuple((await amember_coverage_gaps(user.pk, tenants)).values())


def missing_tenants_by_workspace(user, workspaces) -> dict:
    """Bulk :func:`missing_workspace_tenants`, keyed by workspace id.

    ``workspaces`` must have ``workspace_tenants__tenant`` prefetched. Readiness is
    evaluated once over the union of tenants, so the list endpoint's
    ``has_access`` agrees with the per-request gate without a query per row.
    """
    tenants_by_ws = {ws.id: [wt.tenant for wt in ws.workspace_tenants.all()] for ws in workspaces}
    all_tenants = {t.pk: t for tenants in tenants_by_ws.values() for t in tenants}
    if not all_tenants:
        return dict.fromkeys(tenants_by_ws, ())
    live = set()
    if not all_of_access_enforced():
        live = set(
            TenantMembership.objects.filter(
                user=user, tenant_id__in=all_tenants.keys()
            ).values_list("tenant_id", flat=True)
        )
    # Any-of already grants these, so readiness is only worth computing for the rest.
    granted = {ws_id for ws_id, tenants in tenants_by_ws.items() if live & {t.pk for t in tenants}}
    unresolved = {
        t.pk: t for ws_id, tenants in tenants_by_ws.items() if ws_id not in granted for t in tenants
    }
    gaps = member_coverage_gaps(user.pk, unresolved.values()) if unresolved else {}
    return {
        ws_id: ()
        if ws_id in granted
        else tuple(gaps[str(t.pk)] for t in tenants if str(t.pk) in gaps)
        for ws_id, tenants in tenants_by_ws.items()
    }


def missing_tenants_for_member(user, workspace) -> tuple[MissingTenant, ...]:
    """:func:`missing_workspace_tenants` for one workspace, for handlers that
    resolved with ``require_coverage=False`` and must narrow what they allow."""
    return missing_workspace_tenants(user, _workspace_tenants(workspace))


def _workspace_tenants(workspace) -> list:
    return [wt.tenant for wt in workspace.workspace_tenants.select_related("tenant")]


async def _aworkspace_tenants(workspace) -> list:
    return [wt.tenant async for wt in workspace.workspace_tenants.select_related("tenant")]


def _role_satisfies(role: str, minimum_role: str) -> bool:
    role_rank = _ROLE_RANK.get(role)
    minimum_rank = _ROLE_RANK.get(minimum_role)
    return role_rank is not None and minimum_rank is not None and role_rank >= minimum_rank


def _resolve_local_access_ex(
    user, workspace_id, *, minimum_role: str, require_coverage: bool
) -> WorkspaceAccess:
    """Membership, tenant coverage and role, from local state only."""
    try:
        wm = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return WorkspaceAccess(denied_reason=NOT_MEMBER)
    missing = (
        missing_workspace_tenants(user, _workspace_tenants(wm.workspace))
        if require_coverage
        else ()
    )
    if missing:
        return WorkspaceAccess(denied_reason=TENANT_ACCESS_LOST, missing_tenants=missing)
    if not _role_satisfies(wm.role, minimum_role):
        return WorkspaceAccess(denied_reason=INSUFFICIENT_ROLE)
    return WorkspaceAccess(workspace=wm.workspace, membership=wm)


async def _aresolve_local_access_ex(user, workspace_id, *, minimum_role: str) -> WorkspaceAccess:
    """Async twin of ``_resolve_local_access_ex`` (always requires coverage)."""
    try:
        wm = await WorkspaceMembership.objects.select_related("workspace").aget(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return WorkspaceAccess(denied_reason=NOT_MEMBER)
    missing = await amissing_workspace_tenants(user, await _aworkspace_tenants(wm.workspace))
    if missing:
        return WorkspaceAccess(denied_reason=TENANT_ACCESS_LOST, missing_tenants=missing)
    if not _role_satisfies(wm.role, minimum_role):
        return WorkspaceAccess(denied_reason=INSUFFICIENT_ROLE)
    return WorkspaceAccess(workspace=wm.workspace, membership=wm)


def resolve_workspace_access_ex(
    user,
    workspace_id,
    *,
    minimum_role: str = WorkspaceRole.READ,
    verification: VerificationBudget | None = VerificationBudget.INTERACTIVE,
    require_coverage: bool = True,
) -> WorkspaceAccess:
    """Resolve access, exposing the denial reason (see ``WorkspaceAccess``).

    ``verification`` selects the upstream-freshness budget for protected data;
    ``None`` is the recovery-metadata mode, which needs membership but must stay
    reachable while upstream verification is failing.

    ``require_coverage=False`` is only for the few remediation actions that read
    no tenant data (remove a missing source, leave, hand the manager role to
    another member, delete a workspace nobody else is in, open its page). Without
    it a member who lost a source for good could never get out of the state, since
    the fix itself would be refused (ACCESS-CONTRACT §5). The exemption applies only
    when coverage is what denied: a covered caller still goes through freshness,
    and a caller let in by the exemption skips it (it rechecks the coverage they
    lack). With the all-of switch off there is no exemption at all.
    """
    result = _resolve_with_freshness(
        user, workspace_id, minimum_role=minimum_role, verification=verification
    )
    if require_coverage or not all_of_access_enforced() or not result.missing_tenants:
        return result
    return _resolve_local_access_ex(
        user, workspace_id, minimum_role=minimum_role, require_coverage=False
    )


def _resolve_with_freshness(
    user, workspace_id, *, minimum_role: str, verification: VerificationBudget | None
) -> WorkspaceAccess:
    def local():
        return _resolve_local_access_ex(
            user, workspace_id, minimum_role=minimum_role, require_coverage=True
        )

    result = local()
    if verification is None or not result.granted or not freshness_enforced():
        return result
    admission = admit_upstream(user.pk, _live_tenant_ids(result.workspace), budget=verification)
    if not admission.rechecked:
        return result if admission.admitted else _freshness_denied(admission.reason)
    result = local()
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
    """Async twin of ``resolve_workspace_access_ex``.

    No ``require_coverage`` here: the remediation actions it exists for are all
    sync DRF views, and async callers are data paths that must always check it.
    """
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
