"""Shared workspace resolution for workspace-scoped API views."""

from rest_framework import status
from rest_framework.response import Response

from apps.projects.models import WorkspaceMembership


def resolve_workspace(request, workspace_id):
    """Resolve Workspace from workspace_id URL path parameter.

    workspace_id is the Workspace.id (UUID) and the requesting user must be a member.
    Returns (workspace, membership, None) on success or (None, None, Response(403)) on error.
    """
    try:
        membership = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id,
            user=request.user,
        )
    except WorkspaceMembership.DoesNotExist:
        return (
            None,
            None,
            Response(
                {"error": "Workspace not found or access denied."},
                status=status.HTTP_403_FORBIDDEN,
            ),
        )
    return membership.workspace, membership, None
