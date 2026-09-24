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
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.users.models import TenantMembership
from apps.workspaces import access_cache
from apps.workspaces.models import WorkspaceMembership, WorkspaceRole

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
    ``INSUFFICIENT_ROLE``;
    ``lost_tenant_names`` names the workspace's tenants the user no longer shares.
    """

    workspace: object | None = None
    membership: object | None = None
    denied_reason: str | None = None
    lost_tenant_names: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        return self.workspace is not None


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
                f"You no longer have access to: {projects}. "
                "Access may have been removed upstream — reconnect or ask an admin."
            ),
            "reason": TENANT_ACCESS_LOST,
            "lost_tenants": list(result.lost_tenant_names),
        }
    return {"error": _GENERIC_DENIED}


def _live_tenant_ids(workspace) -> list:
    return list(workspace.workspace_tenants.values_list("tenant_id", flat=True))


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


def resolve_workspace_access_ex(
    user, workspace_id, *, minimum_role: str = WorkspaceRole.READ
) -> WorkspaceAccess:
    """Resolve access, exposing the denial reason (see ``WorkspaceAccess``)."""
    options = (minimum_role,)
    cached = access_cache.lookup(user, workspace_id, options)
    if cached is None:
        cached = _resolve_workspace_access_ex(user, workspace_id, minimum_role=minimum_role)
        access_cache.store(user, workspace_id, options, cached)
    return cached


def _resolve_workspace_access_ex(user, workspace_id, *, minimum_role: str) -> WorkspaceAccess:
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


async def aresolve_workspace_access_ex(
    user, workspace_id, *, minimum_role: str = WorkspaceRole.READ
) -> WorkspaceAccess:
    """Async: resolve access, exposing the denial reason (see ``WorkspaceAccess``)."""
    options = (minimum_role,)
    cached = access_cache.lookup(user, workspace_id, options)
    if cached is None:
        cached = await _aresolve_workspace_access_ex(user, workspace_id, minimum_role=minimum_role)
        access_cache.store(user, workspace_id, options, cached)
    return cached


async def _aresolve_workspace_access_ex(
    user, workspace_id, *, minimum_role: str
) -> WorkspaceAccess:
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
