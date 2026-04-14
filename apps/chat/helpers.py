"""Shared helpers for chat views."""

from apps.users.decorators import (  # noqa: F401 — re-exported for backwards compat
    LoginRequiredJsonMixin,
    async_login_required,
    get_user_if_authenticated,
    login_required_json,
)
from apps.workspaces.models import WorkspaceMembership


async def _resolve_workspace_and_membership(user, workspace_id):
    """Resolve workspace access for a user.

    Returns (workspace, tenant_membership, is_multi_tenant):
    - (None, None, False): workspace not found or user lacks WorkspaceMembership
    - (workspace, None, True): multi-tenant workspace; WorkspaceMembership is sufficient
    - (workspace, None, False): single-tenant workspace but user lacks TenantMembership
    - (workspace, tm, False): single-tenant workspace with a valid TenantMembership
    """
    try:
        wm = await WorkspaceMembership.objects.select_related("workspace").aget(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None, False

    workspace = wm.workspace

    is_multi_tenant = await workspace.workspace_tenants.acount() > 1
    if is_multi_tenant:
        return workspace, None, True

    tenant = await workspace.tenants.afirst()
    if tenant is None:
        return workspace, None, False

    from apps.users.models import TenantMembership

    try:
        tm = await TenantMembership.objects.aget(user=user, tenant=tenant)
    except TenantMembership.DoesNotExist:
        return workspace, None, False
    return workspace, tm, False
