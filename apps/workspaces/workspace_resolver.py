"""Shared workspace resolution for workspace-scoped API views.

Thin adapters over the single authorizer in ``apps.workspaces.access``: they only
translate its access result into each view layer's expected error shape. The
access decision — WorkspaceMembership AND coverage of every tenant — lives solely in
``access.py``, which also builds the 403 body (generic vs. missing sources).
"""

from django.http import JsonResponse
from rest_framework import status
from rest_framework.response import Response

from apps.workspaces.access import (
    access_denied_body,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import WorkspaceRole
from apps.workspaces.services.access_freshness import VerificationBudget


def resolve_workspace_drf(
    request,
    workspace_id,
    *,
    minimum_role: str = WorkspaceRole.READ,
    require_coverage: bool = True,
):
    """Resolve Workspace from workspace_id URL path parameter (DRF views).

    Returns (workspace, membership, None) on success or (None, None, Response(403)) on error.
    ``require_coverage`` is for remediation actions only; see ``resolve_workspace_access_ex``.
    """
    result = resolve_workspace_access_ex(
        request.user,
        workspace_id,
        minimum_role=minimum_role,
        require_coverage=require_coverage,
    )
    if not result.granted:
        return None, None, Response(access_denied_body(result), status=status.HTTP_403_FORBIDDEN)
    return result.workspace, result.membership, None


def resolve_workspace(user, workspace_id, *, minimum_role: str = WorkspaceRole.READ):
    """Resolve Workspace for non-DRF views (sync).

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    result = resolve_workspace_access_ex(user, workspace_id, minimum_role=minimum_role)
    if not result.granted:
        return None, JsonResponse(access_denied_body(result), status=403)
    return result.workspace, None


async def aresolve_workspace(
    user,
    workspace_id,
    *,
    minimum_role: str = WorkspaceRole.READ,
    verification: VerificationBudget | None = VerificationBudget.INTERACTIVE,
):
    """Resolve Workspace for async non-DRF views.

    Returns (workspace, None) on success or (None, JsonResponse(403)) on error.
    """
    result = await aresolve_workspace_access_ex(
        user, workspace_id, minimum_role=minimum_role, verification=verification
    )
    if not result.granted:
        return None, JsonResponse(access_denied_body(result), status=403)
    return result.workspace, None
