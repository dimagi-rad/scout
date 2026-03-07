"""DRF permission classes for workspace role-based access control."""

from rest_framework.permissions import BasePermission

from apps.projects.models import WorkspaceMembership, WorkspaceRole


def _get_membership(request, view):
    workspace_id = view.kwargs.get("workspace_id")
    if not workspace_id:
        return None
    try:
        return WorkspaceMembership.objects.get(
            workspace_id=workspace_id,
            user=request.user,
        )
    except WorkspaceMembership.DoesNotExist:
        return None


class IsWorkspaceMember(BasePermission):
    """Allows any workspace member (any role)."""

    def has_permission(self, request, view):
        return _get_membership(request, view) is not None


class IsWorkspaceReadWrite(BasePermission):
    """Allows read_write and manage role members."""

    def has_permission(self, request, view):
        m = _get_membership(request, view)
        return m is not None and m.role in (WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE)


class IsWorkspaceManager(BasePermission):
    """Allows manage role members only."""

    def has_permission(self, request, view):
        m = _get_membership(request, view)
        return m is not None and m.role == WorkspaceRole.MANAGE
