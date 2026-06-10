"""Tenant management views."""

from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.users.adapters import encrypt_credential
from apps.users.decorators import async_login_required
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.api_key_providers import (
    STRATEGIES,
    CredentialVerificationError,
)
from apps.users.services.ocs_team import adetect_team_from_api_key
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.workspaces.models import Workspace

TENANT_REFRESH_TTL = 3600  # seconds (1 hour)

logger = logging.getLogger(__name__)


async def _aget_token_value(user, provider: str) -> str | None:
    """Return the user's OAuth access token string for *provider*, or None."""
    from apps.users.services.credential_resolver import _social_token_qs

    token = await _social_token_qs(user, provider).afirst()
    return token.token if token else None


# Wrap the persistence loop in sync_to_async so transaction.atomic() applies.
# Django doesn't yet expose async-native transaction support, so this is the
# sanctioned bridge for transactional ORM writes from async views.
@sync_to_async
def _persist_api_key_connection(user, provider, descriptors, encrypted, team_slug, team_name):
    """Create one API-key connection and link every chatbot it discovered to it."""
    rows = []
    with transaction.atomic():
        conn = TenantConnection.objects.create(
            user=user,
            provider=provider,
            credential_type=TenantConnection.API_KEY,
            encrypted_credential=encrypted,
        )
        for desc in descriptors:
            tenant, _ = Tenant.objects.get_or_create(
                provider=provider,
                external_id=desc.external_id,
                defaults={"canonical_name": desc.canonical_name},
            )
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
            tm.connection = conn
            tm.team_slug = team_slug
            tm.team_name = team_name
            tm.archived_at = None
            tm.save(update_fields=["connection", "provider_metadata", "archived_at"])
            rows.append(
                {
                    "membership_id": str(tm.id),
                    "tenant_id": tenant.external_id,
                    "tenant_name": tenant.canonical_name,
                }
            )
    return rows


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
        access_token = await _aget_token_value(user, "commcare")
        if access_token:
            try:
                await resolve_commcare_domains(user, access_token)
                await cache.aset(commcare_cache_key, True, TENANT_REFRESH_TTL)
            except Exception:
                logger.warning("Failed to refresh CommCare domains", exc_info=True)

    # Refresh opportunities from Connect if the user has a Connect OAuth token
    connect_cache_key = f"tenant_refresh:{user.id}:commcare_connect"
    if not await cache.aget(connect_cache_key):
        connect_token = await _aget_token_value(user, "commcare_connect")
        if connect_token:
            try:
                await resolve_connect_opportunities(user, connect_token)
                await cache.aset(connect_cache_key, True, TENANT_REFRESH_TTL)
            except Exception:
                logger.warning("Failed to refresh Connect opportunities", exc_info=True)

    # Refresh chatbots from OCS if the user has an OCS OAuth token
    ocs_cache_key = f"tenant_refresh:{user.id}:ocs"
    if not await cache.aget(ocs_cache_key):
        ocs_token = await _aget_token_value(user, "ocs")
        if ocs_token:
            try:
                await resolve_ocs_chatbots(user, ocs_token)
                await cache.aset(ocs_cache_key, True, TENANT_REFRESH_TTL)
            except Exception:
                logger.warning("Failed to refresh OCS chatbots", exc_info=True)

    memberships = []
    async for tm in TenantMembership.objects.filter(
        user=user, archived_at__isnull=True
    ).select_related("tenant"):
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


@require_http_methods(["GET"])
@async_login_required
async def api_key_providers_view(request):
    """GET /api/auth/api-key-providers/ — list registered API-key strategies
    so the frontend can render the Add/Edit dialog dynamically."""
    payload = [
        {
            "id": strategy.provider_id,
            "display_name": strategy.display_name,
            "fields": list(strategy.form_fields),
        }
        for strategy in STRATEGIES.values()
    ]
    return JsonResponse(payload, safe=False)


@require_http_methods(["GET", "POST"])
@async_login_required
async def tenant_credential_list_view(request):
    """GET  /api/auth/connections/ — list the user's connections, chatbots grouped
    POST /api/auth/connections/ — add a new API-key connection"""
    user = request._authenticated_user

    if request.method == "GET":
        results = []
        async for conn in TenantConnection.objects.filter(user=user).order_by("-created_at"):
            chatbots = []
            async for tm in conn.memberships.filter(archived_at__isnull=True).select_related(
                "tenant"
            ):
                chatbots.append(
                    {
                        "membership_id": str(tm.id),
                        "tenant_id": tm.tenant.external_id,
                        "tenant_name": tm.tenant.canonical_name,
                        "team_slug": tm.team_slug,
                        "team_name": tm.team_name,
                    }
                )
            results.append(
                {
                    "connection_id": str(conn.id),
                    "provider": conn.provider,
                    "credential_type": conn.credential_type,
                    "chatbots": chatbots,
                }
            )
        return JsonResponse(results, safe=False)

    # POST — create an API-key connection via strategy registry
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    provider = body.get("provider", "").strip()
    fields = body.get("fields") or {}

    strategy = STRATEGIES.get(provider)
    if strategy is None:
        return JsonResponse({"error": f"Unknown provider '{provider}'"}, status=400)

    missing = [
        f["key"]
        for f in strategy.form_fields
        if f["required"] and not (fields.get(f["key"]) or "").strip()
    ]
    if missing:
        return JsonResponse(
            {"error": f"Missing required field(s): {', '.join(missing)}"},
            status=400,
        )

    try:
        descriptors = await strategy.verify_and_discover(fields)
    except CredentialVerificationError as e:
        return JsonResponse({"error": str(e)}, status=400)

    # OCS connections are labeled by team. Auto-detect it from the live API;
    # fall back to a user-supplied team name when the team has no sessions.
    team_slug, team_name = "", ""
    if provider == "ocs":
        detected = await adetect_team_from_api_key(fields.get("api_key", ""))
        if detected:
            team_slug, team_name = detected
        else:
            team_name = (fields.get("team_name") or "").strip()
            if not team_name:
                return JsonResponse(
                    {"error": "Could not detect the OCS team; enter a team name."},
                    status=400,
                )

    try:
        packed = strategy.pack_credential(fields)
        encrypted = encrypt_credential(packed)
    except (KeyError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=500)

    try:
        memberships_payload = await _persist_api_key_connection(
            user, provider, descriptors, encrypted, team_slug, team_name
        )
    except Exception as e:
        logger.exception("Failed to persist connection for provider %s", provider)
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"memberships": memberships_payload}, status=201)


@sync_to_async
def _archive_and_delete_connection(conn):
    """Archive the connection's live memberships (retaining data), then delete it."""
    with transaction.atomic():
        conn.memberships.filter(archived_at__isnull=True).update(
            archived_at=timezone.now(), connection=None
        )
        conn.delete()


@require_http_methods(["DELETE", "PATCH"])
@async_login_required
async def connection_detail_view(request, connection_id):
    """DELETE /api/auth/connections/<id>/ — remove a connection (archives its chatbots)
    PATCH  /api/auth/connections/<id>/ — rotate the connection's API key"""
    user = request._authenticated_user

    try:
        conn = await TenantConnection.objects.aget(id=connection_id, user=user)
    except (TenantConnection.DoesNotExist, ValueError, ValidationError):
        return JsonResponse({"error": "Not found"}, status=404)

    if request.method == "DELETE":
        await _archive_and_delete_connection(conn)
        return JsonResponse({"status": "removed"})

    # PATCH — rotate API key via strategy registry
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    fields = body.get("fields") or {}

    strategy = STRATEGIES.get(conn.provider)
    if strategy is None:
        return JsonResponse(
            {"error": f"Provider '{conn.provider}' has no API-key strategy"},
            status=400,
        )

    editable = [f for f in strategy.form_fields if f["editable_on_rotate"]]
    missing = [
        f["key"] for f in editable if f["required"] and not (fields.get(f["key"]) or "").strip()
    ]
    if missing:
        return JsonResponse(
            {"error": f"Missing required field(s): {', '.join(missing)}"},
            status=400,
        )

    # Verify the new key still has access to one of this connection's chatbots.
    sample = await conn.memberships.select_related("tenant").afirst()
    if sample is None:
        return JsonResponse(
            {"error": "Connection has no linked data sources to verify against"}, status=400
        )
    try:
        await strategy.verify_for_tenant(fields, external_id=sample.tenant.external_id)
    except CredentialVerificationError as e:
        return JsonResponse({"error": str(e)}, status=400)

    try:
        packed = strategy.pack_credential(fields)
        encrypted = encrypt_credential(packed)
    except (KeyError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    conn.encrypted_credential = encrypted
    await conn.asave(update_fields=["encrypted_credential"])
    return JsonResponse({"connection_id": str(conn.id), "provider": conn.provider})


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
            connect_token = await _aget_token_value(user, "commcare_connect")
            if not connect_token:
                return JsonResponse(
                    {"error": "No Connect OAuth token. Please log in with Connect first."},
                    status=404,
                )

            # Resolve the user's actual opportunities from the Connect API
            # to verify they have access to the requested tenant_id.
            memberships = await resolve_connect_opportunities(user, connect_token)
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
