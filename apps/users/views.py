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

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    tenant_membership_id = body.get("tenant_id")

    try:
        tm = await TenantMembership.objects.aget(id=tenant_membership_id, user=user)
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found"}, status=404)

    tm.last_selected_at = timezone.now()
    await tm.asave(update_fields=["last_selected_at"])

    return JsonResponse({"status": "ok", "tenant_id": tm.tenant_id})


@require_http_methods(["GET", "POST"])
async def tenant_credential_list_view(request):
    """GET  /api/auth/tenant-credentials/ — list configured tenant credentials
    POST /api/auth/tenant-credentials/ — create a new API-key-based tenant"""
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    if request.method == "GET":
        results = []
        async for tm in TenantMembership.objects.filter(
            user=user,
            credential__isnull=False,
        ).select_related("credential"):
            results.append(
                {
                    "membership_id": str(tm.id),
                    "provider": tm.provider,
                    "tenant_id": tm.tenant_id,
                    "tenant_name": tm.tenant_name,
                    "credential_type": tm.credential.credential_type,
                }
            )
        return JsonResponse(results, safe=False)

    # POST — create API-key-backed membership
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    provider = body.get("provider", "").strip()
    tenant_id = body.get("tenant_id", "").strip()
    tenant_name = body.get("tenant_name", "").strip()
    credential = body.get("credential", "").strip()

    if not all([provider, tenant_id, tenant_name, credential]):
        return JsonResponse(
            {"error": "provider, tenant_id, tenant_name, and credential are required"},
            status=400,
        )

    from django.db import transaction

    from apps.users.adapters import encrypt_credential
    from apps.users.models import TenantCredential

    try:
        encrypted = await sync_to_async(encrypt_credential)(credential)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=500)

    def _create():
        with transaction.atomic():
            tm, _ = TenantMembership.objects.update_or_create(
                user=user,
                provider=provider,
                tenant_id=tenant_id,
                defaults={"tenant_name": tenant_name},
            )
            TenantCredential.objects.update_or_create(
                tenant_membership=tm,
                defaults={
                    "credential_type": TenantCredential.API_KEY,
                    "encrypted_credential": encrypted,
                },
            )
            return tm

    tm = await sync_to_async(_create)()
    return JsonResponse({"membership_id": str(tm.id)}, status=201)


@require_http_methods(["DELETE", "PATCH"])
async def tenant_credential_detail_view(request, membership_id):
    """DELETE /api/auth/tenant-credentials/<membership_id>/ — remove a credential
    PATCH  /api/auth/tenant-credentials/<membership_id>/ — update credential"""
    user = await _get_user_if_authenticated(request)
    if user is None:
        return JsonResponse({"error": "Authentication required"}, status=401)

    if request.method == "DELETE":
        def _delete():
            try:
                tm = TenantMembership.objects.get(id=membership_id, user=user)
                tm.delete()  # cascades to TenantCredential
                return True
            except TenantMembership.DoesNotExist:
                return False

        deleted = await sync_to_async(_delete)()
        if not deleted:
            return JsonResponse({"error": "Not found"}, status=404)
        return JsonResponse({"status": "deleted"})

    # PATCH
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    tenant_name = body.get("tenant_name", "").strip()
    credential = body.get("credential", "").strip()

    if not tenant_name and not credential:
        return JsonResponse(
            {"error": "At least one of tenant_name or credential is required"}, status=400
        )

    from apps.users.adapters import encrypt_credential

    encrypted = None
    if credential:
        try:
            encrypted = await sync_to_async(encrypt_credential)(credential)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=500)

    def _update():
        try:
            tm = TenantMembership.objects.select_related("credential").get(
                id=membership_id, user=user
            )
        except TenantMembership.DoesNotExist:
            return None

        if tenant_name:
            tm.tenant_name = tenant_name
            tm.save(update_fields=["tenant_name"])

        if encrypted and hasattr(tm, "credential"):
            tm.credential.encrypted_credential = encrypted
            tm.credential.save(update_fields=["encrypted_credential"])

        return tm

    tm = await sync_to_async(_update)()
    if tm is None:
        return JsonResponse({"error": "Not found"}, status=404)

    return JsonResponse({"membership_id": str(tm.id), "tenant_name": tm.tenant_name})
