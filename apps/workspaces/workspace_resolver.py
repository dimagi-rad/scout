"""Shared workspace resolution for workspace-scoped API views."""

from django.http import JsonResponse
from rest_framework import status
from rest_framework.response import Response

from apps.workspaces.models import WorkspaceMembership

_ACCESS_DENIED = {"error": "Workspace not found or access denied."}


def resolve_workspace_drf(request, workspace_id):
    """Resolve Workspace from workspace_id URL path parameter (DRF views).

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
                _ACCESS_DENIED,
                status=status.HTTP_403_FORBIDDEN,
            ),
        )
    return membership.workspace, membership, None


def resolve_workspace(user, workspace_id):
    """Resolve Workspace for non-DRF views (sync).

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    try:
        membership = WorkspaceMembership.objects.select_related("workspace").get(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, JsonResponse(_ACCESS_DENIED, status=403)
    return membership.workspace, None


async def aresolve_workspace(user, workspace_id):
    """Resolve Workspace for async non-DRF views.

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    try:
        membership = await WorkspaceMembership.objects.select_related("workspace").aget(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, JsonResponse(_ACCESS_DENIED, status=403)
    return membership.workspace, None
