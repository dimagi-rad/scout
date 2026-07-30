"""Shared workspace resolution for workspace-scoped API views.

Thin adapters over the single authorizer in ``apps.workspaces.access``: they only
translate its access result into each view layer's expected error shape. The
access decision — WorkspaceMembership AND a live tenant — lives solely in
``access.py``, which also builds the 403 body (generic vs. lost-upstream-access).
"""

from django.http import JsonResponse
from rest_framework import status
from rest_framework.response import Response

from apps.workspaces.access import (
    access_denied_body,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)


def resolve_workspace_drf(request, workspace_id):
    """Resolve Workspace from workspace_id URL path parameter (DRF views).

    Returns (workspace, membership, None) on success or (None, None, Response(403)) on error.
    """
    result = resolve_workspace_access_ex(request.user, workspace_id)
    if not result.granted:
        return None, None, Response(access_denied_body(result), status=status.HTTP_403_FORBIDDEN)
    return result.workspace, result.membership, None


def resolve_workspace(user, workspace_id):
    """Resolve Workspace for non-DRF views (sync).

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    result = resolve_workspace_access_ex(user, workspace_id)
    if not result.granted:
        return None, JsonResponse(access_denied_body(result), status=403)
    return result.workspace, None


async def aresolve_workspace(user, workspace_id):
    """Resolve Workspace for async non-DRF views.

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    result = await aresolve_workspace_access_ex(user, workspace_id)
    if not result.granted:
        return None, JsonResponse(access_denied_body(result), status=403)
    return result.workspace, None
