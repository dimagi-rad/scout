"""Tenant management views."""

from __future__ import annotations

import json
import logging

from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.users.models import TenantMembership

logger = logging.getLogger(__name__)


@sync_to_async
def _get_user_if_authenticated(request):
    if request.user.is_authenticated:
        return request.user
    return None


def _get_commcare_token(user) -> str | None:
    """Return the user's CommCare OAuth access token, or None."""
    token = (
        SocialToken.objects.filter(
            account__user=user,
            account__provider__startswith="commcare",
        )
        .exclude(account__provider__startswith="commcare_connect")
        .first()
    )
    return token.token if token else None


@require_http_methods(["GET"])
async def tenant_list_view(request):
    """GET /api/auth/tenants/ — List the user's tenant memberships.

    If the user has a CommCare OAuth token, refreshes domain list from
    CommCare API before returning results.
    """
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    # Refresh domains from CommCare if the user has an OAuth token
    access_token = await sync_to_async(_get_commcare_token)(user)
    if access_token:
        try:
            from apps.users.services.tenant_resolution import resolve_commcare_domains

            await sync_to_async(resolve_commcare_domains)(user, access_token)
        except Exception:
            logger.warning("Failed to refresh CommCare domains", exc_info=True)

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
