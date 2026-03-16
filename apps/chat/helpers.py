"""Shared helpers for chat views."""

from asgiref.sync import sync_to_async

from apps.projects.models import WorkspaceMembership
from apps.users.decorators import (  # noqa: F401 — re-exported for backwards compat
    LoginRequiredJsonMixin,
    async_login_required,
    get_user_if_authenticated,
    login_required_json,
)


@sync_to_async
def _resolve_workspace_and_membership(user, workspace_id):
    """Resolve workspace access for a user.

    Returns (workspace, tenant_membership, is_multi_tenant):
    - (None, None, False): workspace not found or user lacks WorkspaceMembership
    - (workspace, None, True): multi-tenant workspace; WorkspaceMembership is sufficient
    - (workspace, None, False): single-tenant workspace but user lacks TenantMembership
    - (workspace, tm, False): single-tenant workspace with a valid TenantMembership
    """
    try:
        wm = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None, False

    workspace = wm.workspace

    # Read tenant count exactly once so callers don't need a second DB query.
    # Multi-tenant workspaces grant access by WorkspaceMembership alone;
    # TenantMembership is irrelevant (and must not be checked) for multi-tenant access.
    is_multi_tenant = workspace.workspace_tenants.count() > 1
    if is_multi_tenant:
        return workspace, None, True

    tenant = workspace.tenant
    if tenant is None:
        return workspace, None, False

    from apps.users.models import TenantMembership

    try:
        tm = TenantMembership.objects.get(user=user, tenant=tenant)
    except TenantMembership.DoesNotExist:
        return workspace, None, False
    return workspace, tm, False
