"""Tenant management views."""

from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.users.decorators import async_login_required
from apps.users.models import Tenant, TenantMembership
from apps.users.services.credential_resolver import get_social_token
from apps.users.services.tenant_verification import (
    CommCareVerificationError,
    verify_commcare_credential,
)

TENANT_REFRESH_TTL = 3600  # seconds (1 hour)

logger = logging.getLogger(__name__)


def _get_token_value(user, provider: str) -> str | None:
    """Return the user's OAuth access token string for *provider*, or None."""
    token = get_social_token(user, provider)
    return token.token if token else None


@require_http_methods(["GET"])
@async_login_required
async def tenant_list_view(request):
    """GET /api/auth/tenants/ — List the user's tenant memberships.

    If the user has a CommCare OAuth token, refreshes domain list from
    CommCare API before returning results.
    """
    user = request._authenticated_user

    # Refresh domains from CommCare if the user has an OAuth token
    commcare_cache_key = f"tenant_refresh:{user.id}:commcare"
    if not await cache.aget(commcare_cache_key):
        access_token = await sync_to_async(_get_token_value)(user, "commcare")
        if access_token:
            try:
                from apps.users.services.tenant_resolution import resolve_commcare_domains

                await sync_to_async(resolve_commcare_domains)(user, access_token)
                await cache.aset(commcare_cache_key, True, TENANT_REFRESH_TTL)
            except Exception:
                logger.warning("Failed to refresh CommCare domains", exc_info=True)

    # Refresh opportunities from Connect if the user has a Connect OAuth token
    connect_cache_key = f"tenant_refresh:{user.id}:commcare_connect"
    if not await cache.aget(connect_cache_key):
        connect_token = await sync_to_async(_get_token_value)(user, "commcare_connect")
        if connect_token:
            try:
                from apps.users.services.tenant_resolution import resolve_connect_opportunities

                await sync_to_async(resolve_connect_opportunities)(user, connect_token)
                await cache.aset(connect_cache_key, True, TENANT_REFRESH_TTL)
            except Exception:
                logger.warning("Failed to refresh Connect opportunities", exc_info=True)

    memberships = []
    async for tm in TenantMembership.objects.filter(user=user).select_related("tenant"):
        memberships.append(
            {
                "id": str(tm.id),
                "provider": tm.tenant.provider,
                "tenant_id": tm.tenant.external_id,
                "tenant_uuid": str(tm.tenant.id),
                "tenant_name": tm.tenant.canonical_name,
                "last_selected_at": (
                    tm.last_selected_at.isoformat() if tm.last_selected_at else None
                ),
            }
        )

    return JsonResponse(memberships, safe=False)


# last_selected_at is a UX ordering hint only.
# It does NOT affect API workspace resolution — all resource endpoints
# use explicit tenant_id path parameters.
@require_http_methods(["POST"])
@async_login_required
async def tenant_select_view(request):
    """POST /api/auth/tenants/select/ — Mark a tenant as the active selection."""
    user = request._authenticated_user

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    tenant_membership_id = body.get("tenant_id")

    try:
        tm = await TenantMembership.objects.select_related("tenant").aget(
            id=tenant_membership_id, user=user
        )
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Tenant not found"}, status=404)

    tm.last_selected_at = timezone.now()
    await tm.asave(update_fields=["last_selected_at"])

    return JsonResponse({"status": "ok", "tenant_id": tm.tenant.external_id})


@require_http_methods(["GET", "POST"])
@async_login_required
async def tenant_credential_list_view(request):
    """GET  /api/auth/tenant-credentials/ — list configured tenant credentials
    POST /api/auth/tenant-credentials/ — create a new API-key-based tenant"""
    user = request._authenticated_user

    if request.method == "GET":
        results = []
        async for tm in TenantMembership.objects.filter(
            user=user,
            credential__isnull=False,
        ).select_related("credential", "tenant"):
            results.append(
                {
                    "membership_id": str(tm.id),
                    "provider": tm.tenant.provider,
                    "tenant_id": tm.tenant.external_id,
                    "tenant_name": tm.tenant.canonical_name,
                    "credential_type": tm.credential.credential_type,
                }
            )
        return JsonResponse(results, safe=False)

    # POST — create API-key-backed membership with provider verification
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

    if provider != "commcare":
        return JsonResponse(
            {"error": f"API-key credentials are not supported for provider '{provider}'"},
            status=400,
        )

    # credential must be "username:apikey"
    if ":" not in credential:
        return JsonResponse(
            {"error": "credential must be in the format 'username:apikey'"},
            status=400,
        )
    cc_username, cc_api_key = credential.split(":", 1)

    try:
        await sync_to_async(verify_commcare_credential)(
            domain=tenant_id, username=cc_username, api_key=cc_api_key
        )
    except CommCareVerificationError as e:
        return JsonResponse({"error": str(e)}, status=400)

    from django.db import transaction

    from apps.users.adapters import encrypt_credential
    from apps.users.models import TenantCredential

    try:
        encrypted = await sync_to_async(encrypt_credential)(credential)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=500)

    def _create():
        with transaction.atomic():
            # Use get_or_create so that an existing Tenant's canonical_name is never
            # overwritten by a user-supplied string (which feeds into the LLM system prompt).
            tenant, _ = Tenant.objects.get_or_create(
                provider=provider,
                external_id=tenant_id,
                defaults={"canonical_name": tenant_name},
            )
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
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
@async_login_required
async def tenant_credential_detail_view(request, membership_id):
    """DELETE /api/auth/tenant-credentials/<membership_id>/ — remove a credential
    PATCH  /api/auth/tenant-credentials/<membership_id>/ — update credential"""
    user = request._authenticated_user

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

    credential = body.get("credential", "").strip()

    if not credential:
        return JsonResponse({"error": "credential is required"}, status=400)

    if ":" not in credential:
        return JsonResponse(
            {"error": "credential must be in the format 'username:apikey'"},
            status=400,
        )
    cc_username, cc_api_key = credential.split(":", 1)

    # Fetch membership to get tenant domain for verification
    try:
        tm = await TenantMembership.objects.select_related("credential", "tenant").aget(
            id=membership_id, user=user
        )
    except TenantMembership.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    if not hasattr(tm, "credential"):
        return JsonResponse({"error": "Not found"}, status=404)

    try:
        await sync_to_async(verify_commcare_credential)(
            domain=tm.tenant.external_id, username=cc_username, api_key=cc_api_key
        )
    except CommCareVerificationError as e:
        return JsonResponse({"error": str(e)}, status=400)

    from apps.users.adapters import encrypt_credential

    try:
        encrypted = await sync_to_async(encrypt_credential)(credential)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=500)

    def _save_credential(tm):
        tm.credential.encrypted_credential = encrypted
        tm.credential.save(update_fields=["encrypted_credential"])
        return tm

    tm = await sync_to_async(_save_credential)(tm)
    return JsonResponse({"membership_id": str(tm.id), "tenant_name": tm.tenant.canonical_name})


@require_http_methods(["POST"])
@async_login_required
async def tenant_ensure_view(request):
    """POST /api/auth/tenants/ensure/ — Find or create a TenantMembership and select it.

    Used by the embed SDK when an opp ID is passed via URL param. If the user
    has an OAuth token for the provider and no matching membership exists, one
    is created.
    """
    user = request._authenticated_user

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    provider = body.get("provider", "").strip()
    tenant_id = body.get("tenant_id", "").strip()

    if not provider or not tenant_id:
        return JsonResponse({"error": "provider and tenant_id are required"}, status=400)

    # Try to find existing membership
    try:
        tm = await TenantMembership.objects.select_related("tenant").aget(
            user=user, tenant__provider=provider, tenant__external_id=tenant_id
        )
    except TenantMembership.DoesNotExist:
        if provider == "commcare_connect":
            connect_token = await sync_to_async(_get_token_value)(user, "commcare_connect")
            if not connect_token:
                return JsonResponse(
                    {"error": "No Connect OAuth token. Please log in with Connect first."},
                    status=404,
                )

            # Resolve the user's actual opportunities from the Connect API
            # to verify they have access to the requested tenant_id.
            from apps.users.services.tenant_resolution import (
                resolve_connect_opportunities,
            )

            memberships = await sync_to_async(resolve_connect_opportunities)(user, connect_token)
            tm = next((m for m in memberships if m.tenant.external_id == tenant_id), None)
            if tm is None:
                return JsonResponse(
                    {"error": "Opportunity not found for this user"},
                    status=404,
                )
        else:
            return JsonResponse({"error": "Tenant not found"}, status=404)

    tm.last_selected_at = timezone.now()
    await tm.asave(update_fields=["last_selected_at"])

    # Find the auto-created workspace for this tenant
    from apps.workspaces.models import Workspace

    workspace = await Workspace.objects.filter(
        workspace_tenants__tenant=tm.tenant,
        memberships__user=user,
    ).afirst()

    return JsonResponse(
        {
            "id": str(tm.id),
            "provider": tm.tenant.provider,
            "tenant_id": tm.tenant.external_id,
            "tenant_name": tm.tenant.canonical_name,
            "workspace_id": str(workspace.id) if workspace else None,
        }
    )
