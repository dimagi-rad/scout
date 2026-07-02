"""Shared workspace resolution for workspace-scoped API views.

Thin adapters over the single authorizer in ``apps.workspaces.access``: they only
translate its ``(workspace, membership) | (None, None)`` result into each view
layer's expected error shape. The access decision — WorkspaceMembership AND a live
tenant — lives solely in ``access.py``.
"""

from django.http import JsonResponse
from rest_framework import status
from rest_framework.response import Response

from apps.workspaces.access import aresolve_workspace_access, resolve_workspace_access

_ACCESS_DENIED = {"error": "Workspace not found or access denied."}


def resolve_workspace_drf(request, workspace_id):
    """Resolve Workspace from workspace_id URL path parameter (DRF views).

    Returns (workspace, membership, None) on success or (None, None, Response(403)) on error.
    """
    workspace, membership = resolve_workspace_access(request.user, workspace_id)
    if workspace is None:
        return None, None, Response(_ACCESS_DENIED, status=status.HTTP_403_FORBIDDEN)
    return workspace, membership, None


def resolve_workspace(user, workspace_id):
    """Resolve Workspace for non-DRF views (sync).

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    workspace, _membership = resolve_workspace_access(user, workspace_id)
    if workspace is None:
        return None, JsonResponse(_ACCESS_DENIED, status=403)
    return workspace, None


async def aresolve_workspace(user, workspace_id):
    """Resolve Workspace for async non-DRF views.

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    workspace, _membership = await aresolve_workspace_access(user, workspace_id)
    if workspace is None:
        return None, JsonResponse(_ACCESS_DENIED, status=403)
    return workspace, None
