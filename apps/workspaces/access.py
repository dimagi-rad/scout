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
"""

from __future__ import annotations

from apps.users.models import TenantMembership
from apps.workspaces.models import WorkspaceMembership


def _live_tenant_ids(workspace) -> list:
    return list(workspace.workspace_tenants.values_list("tenant_id", flat=True))


async def _alive_tenant_ids(workspace) -> list:
    return [tid async for tid in workspace.workspace_tenants.values_list("tenant_id", flat=True)]


def _shares_live_tenant(user, tenant_ids) -> bool:
    # Zero-tenant workspace: nothing to gate on, WorkspaceMembership suffices.
    if not tenant_ids:
        return True
    return TenantMembership.objects.filter(user=user, tenant_id__in=tenant_ids).exists()


async def _ashares_live_tenant(user, tenant_ids) -> bool:
    if not tenant_ids:
        return True
    return await TenantMembership.objects.filter(user=user, tenant_id__in=tenant_ids).aexists()


def resolve_workspace_access(user, workspace_id):
    """Return ``(workspace, WorkspaceMembership)`` if the user has access, else ``(None, None)``."""
    try:
        wm = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None
    if not _shares_live_tenant(user, _live_tenant_ids(wm.workspace)):
        return None, None
    return wm.workspace, wm


async def aresolve_workspace_access(user, workspace_id):
    """Async: return ``(workspace, WorkspaceMembership)`` on access, else ``(None, None)``."""
    try:
        wm = await WorkspaceMembership.objects.select_related("workspace").aget(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None
    if not await _ashares_live_tenant(user, await _alive_tenant_ids(wm.workspace)):
        return None, None
    return wm.workspace, wm
