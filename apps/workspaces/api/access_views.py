"""Explicit upstream-verification retry for a workspace member.

Deliberately outside the protected gate: a member whose verification is failing
must be able to start recovery without first passing the check that is failing.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from apps.users.decorators import async_login_required
from apps.workspaces.access import access_denied_body, aretry_workspace_verification


@require_POST
@async_login_required
async def workspace_access_verify_view(request, workspace_id):
    """POST /api/workspaces/<id>/access/verify/ — recheck the caller's own upstream access."""
    result = await aretry_workspace_verification(request._authenticated_user, workspace_id)
    if result.granted:
        return JsonResponse({"has_access": True})
    return JsonResponse({"has_access": False, **access_denied_body(result)}, status=403)
