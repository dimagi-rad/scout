"""Async API view for per-source data freshness (the chat's stale-data banner, #173)."""

from django.conf import settings
from django.http import JsonResponse

from apps.users.decorators import async_login_required
from apps.workspaces.services.load_activity import aworkspace_load_pending
from apps.workspaces.services.source_freshness import aworkspace_source_freshness, provider_label
from apps.workspaces.workspace_resolver import aresolve_workspace


@async_login_required
async def source_freshness_view(request, workspace_id):
    """GET /api/workspaces/<workspace_id>/freshness/

    Each source's data age and whether only a reconnect can refresh it, whether a load covering the
    workspace is queued or running, and the age past which the chat offers a refresh.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user
    workspace, err = await aresolve_workspace(user, workspace_id)
    if err is not None:
        return err

    sources = await aworkspace_source_freshness(workspace.id, user.id)
    in_progress = await aworkspace_load_pending(workspace.id)
    return JsonResponse(
        {
            "stale_data_banner_hours": settings.STALE_DATA_BANNER_HOURS,
            "in_progress": in_progress,
            "sources": [
                {
                    "tenant_id": source["tenant_id"],
                    "name": source["name"],
                    "provider": source["provider"],
                    "provider_label": provider_label(source["provider"]),
                    "serving": source["serving"],
                    "last_fetched_at": source["last_fetched_at"],
                    "reconnect": source.get("reconnect", False),
                }
                for source in sources
            ],
        }
    )
