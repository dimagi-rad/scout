"""Tenant management views."""
from __future__ import annotations

import json

from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.users.models import TenantMembership


@sync_to_async
def _get_user_if_authenticated(request):
    if request.user.is_authenticated:
        return request.user
    return None


@require_http_methods(["GET"])
async def tenant_list_view(request):
    """GET /api/auth/tenants/ — List the user's tenant memberships."""
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    memberships = []
    async for tm in TenantMembership.objects.filter(user=user):
        memberships.append(
            {
                "id": str(tm.id),
                "provider": tm.provider,
                "tenant_id": tm.tenant_id,
                "tenant_name": tm.tenant_name,
                "last_selected_at": (
                    tm.last_selected_at.isoformat() if tm.last_selected_at else None
                ),
            }
        )

    return JsonResponse(memberships, safe=False)


@require_http_methods(["POST"])
async def tenant_select_view(request):
    """POST /api/auth/tenants/select/ — Mark a tenant as the active selection."""
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    body = json.loads(request.body)
    tenant_membership_id = body.get("tenant_id")

    try:
        tm = await TenantMembership.objects.aget(id=tenant_membership_id, user=user)
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found"}, status=404)

    tm.last_selected_at = timezone.now()
    await tm.asave(update_fields=["last_selected_at"])

    return JsonResponse({"status": "ok", "tenant_id": tm.tenant_id})
