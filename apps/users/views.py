"""Tenant management views."""

from __future__ import annotations

import json
import logging

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.users.adapters import encrypt_credential
from apps.users.decorators import async_login_required
from apps.users.models import Tenant, TenantCredential, TenantMembership
from apps.users.services.api_key_providers import (
    STRATEGIES,
    CredentialVerificationError,
)
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


async def _extract_ocs_team_info(api_key: str) -> tuple[str | None, str]:
    """Extract OCS team ID and name from an API key by querying OCS API.

    Returns (team_id, team_name) where team_id is the workspace/team ID from
    OCS and team_name is the display name. Returns (None, "") if unable to fetch.
    """
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Try to fetch workspace/team context from OCS
            resp = await client.get(
                f"{base_url}/api/teams/",
                headers={"X-api-key": api_key},
            )
            if resp.status_code == 200:
                team_data = resp.json()
                # OCS may return a list; if so, use the first team
                if isinstance(team_data, list) and team_data:
                    team_id = str(team_data[0].get("id"))
                    team_name = team_data[0].get("name", "")
                    return team_id, team_name
                elif isinstance(team_data, dict) and "id" in team_data:
                    team_id = str(team_data["id"])
                    team_name = team_data.get("name", "")
                    return team_id, team_name
    except Exception as e:
        logger.warning("Failed to extract OCS team info from API: %s", e)
    return None, ""


# Wrap the persistence loop in sync_to_async so transaction.atomic() applies.
# Django doesn't yet expose async-native transaction support, so this is the
# sanctioned bridge for transactional ORM writes from async views.
@sync_to_async
def _persist_api_key_memberships(
    user, provider, descriptors, encrypted, team_id: str | None = None, team_name: str = ""
):
    """Persist API key credentials for multiple tenants.

    For multi-team providers like OCS, team_id is used to distinguish multiple
    API-key credentials per tenant_membership (one per team).

    Args:
        user: The user to associate memberships with
        provider: The provider (e.g., "ocs", "commcare")
        descriptors: List of TenantDescriptor objects with external_id and canonical_name
        encrypted: The encrypted credential string
        team_id: Optional team identifier (for multi-credential support)
        team_name: Optional team display name
    """
    rows = []
    with transaction.atomic():
        for desc in descriptors:
            tenant, _ = Tenant.objects.get_or_create(
                provider=provider,
                external_id=desc.external_id,
                defaults={"canonical_name": desc.canonical_name},
            )
            tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)

            # For multi-team support (e.g., OCS with multiple workspaces),
            # scope to API_KEY credentials to avoid matching/overwriting OAuth rows
            cred_query = {
                "tenant_membership": tm,
                "credential_type": TenantCredential.API_KEY,
            }
            if team_id is not None:
                cred_query["team_id"] = team_id
            else:
                # Default to empty string for team_id when not extracted
                cred_query["team_id"] = ""

            TenantCredential.objects.update_or_create(
                **cred_query,
                defaults={
                    "encrypted_credential": encrypted,
                    "team_name": team_name,
                },
            )
            rows.append(
                {
                    "membership_id": str(tm.id),
                    "tenant_id": tenant.external_id,
                    "tenant_name": tenant.canonical_name,
                    "team_id": team_id,
                    "team_name": team_name,
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
    """GET  /api/auth/tenant-credentials/ — list configured tenant credentials
    POST /api/auth/tenant-credentials/ — create a new API-key-based tenant"""
    user = request._authenticated_user

    if request.method == "GET":
        results = []
        async for tm in TenantMembership.objects.filter(
            user=user,
        ).select_related("tenant"):
            # Each membership can have multiple credentials (for multi-team support)
            async for cred in tm.credentials.all():
                results.append(
                    {
                        "membership_id": str(tm.id),
                        "credential_id": str(cred.id),
                        "provider": tm.tenant.provider,
                        "tenant_id": tm.tenant.external_id,
                        "tenant_name": tm.tenant.canonical_name,
                        "credential_type": cred.credential_type,
                        "team_id": cred.team_id,
                        "team_name": cred.team_name,
                    }
                )
        return JsonResponse(results, safe=False)

    # POST — create API-key-backed membership(s) via strategy registry
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

    try:
        packed = strategy.pack_credential(fields)
        encrypted = encrypt_credential(packed)
    except (KeyError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=500)

    # For OCS, extract team information from the API key
    team_id = None
    team_name = ""
    if provider == "ocs":
        try:
            team_id, team_name = await _extract_ocs_team_info(fields.get("api_key", ""))
        except Exception:
            logger.debug("Failed to extract OCS team info; continuing without team context")

    try:
        memberships_payload = await _persist_api_key_memberships(
            user, provider, descriptors, encrypted, team_id=team_id, team_name=team_name
        )
    except Exception as e:
        logger.exception("Failed to persist memberships for provider %s", provider)
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"memberships": memberships_payload}, status=201)


@require_http_methods(["DELETE", "PATCH"])
@async_login_required
async def tenant_credential_detail_view(request, membership_id, credential_id=None):
    """DELETE /api/auth/tenant-credentials/{credential_id}/ — remove a specific credential
    PATCH  /api/auth/tenant-credentials/{credential_id}/ — update credential

    For backward compat, if credential_id is not in URL, it's inferred from the body.
    """
    user = request._authenticated_user

    if request.method == "DELETE":
        # Support both /tenant-credentials/{membership_id}/ (legacy) and
        # /tenant-credentials/{credential_id}/ (new) URL patterns
        cred_to_delete = None
        if credential_id:
            try:
                cred = await TenantCredential.objects.select_related(
                    "tenant_membership"
                ).aget(
                    id=credential_id,
                    tenant_membership__user=user,
                )
                cred_to_delete = cred
            except TenantCredential.DoesNotExist:
                return JsonResponse({"error": "Not found"}, status=404)
        else:
            # Legacy: membership_id is actually the credential ID or membership ID
            # Try credential first, then fall back to membership
            try:
                cred = await TenantCredential.objects.aget(
                    id=membership_id,
                    tenant_membership__user=user,
                )
                cred_to_delete = cred
            except TenantCredential.DoesNotExist:
                try:
                    tm = await TenantMembership.objects.aget(id=membership_id, user=user)
                    cred_to_delete = None  # Will delete entire membership below
                except TenantMembership.DoesNotExist:
                    return JsonResponse({"error": "Not found"}, status=404)

        if cred_to_delete:
            await cred_to_delete.adelete()
        else:
            # Delete entire membership (legacy behavior)
            tm = await TenantMembership.objects.aget(id=membership_id, user=user)
            await tm.adelete()

        return JsonResponse({"status": "deleted"})

    # PATCH — rotate API key via strategy registry
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    fields = body.get("fields") or {}

    # Find the credential to update
    cred_to_update = None
    tm = None
    if credential_id:
        try:
            cred_to_update = await TenantCredential.objects.select_related(
                "tenant_membership__tenant"
            ).aget(
                id=credential_id,
                tenant_membership__user=user,
            )
            tm = cred_to_update.tenant_membership
        except TenantCredential.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
    else:
        # Legacy: membership_id may actually be credential_id
        try:
            cred_to_update = await TenantCredential.objects.select_related(
                "tenant_membership__tenant"
            ).aget(
                id=membership_id,
                tenant_membership__user=user,
            )
            tm = cred_to_update.tenant_membership
        except TenantCredential.DoesNotExist:
            try:
                tm = await TenantMembership.objects.select_related("tenant").aget(
                    id=membership_id, user=user
                )
                # Get first credential if available
                cred_to_update = await tm.credentials.afirst()
                if not cred_to_update:
                    return JsonResponse({"error": "No credential found"}, status=404)
            except TenantMembership.DoesNotExist:
                return JsonResponse({"error": "Not found"}, status=404)

    strategy = STRATEGIES.get(tm.tenant.provider)
    if strategy is None:
        return JsonResponse(
            {"error": f"Provider '{tm.tenant.provider}' has no API-key strategy"},
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

    try:
        await strategy.verify_for_tenant(fields, external_id=tm.tenant.external_id)
    except CredentialVerificationError as e:
        return JsonResponse({"error": str(e)}, status=400)

    try:
        packed = strategy.pack_credential(fields)
        encrypted = encrypt_credential(packed)
    except (KeyError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=400)

    cred_to_update.encrypted_credential = encrypted
    await cred_to_update.asave(update_fields=["encrypted_credential"])
    return JsonResponse(
        {
            "credential_id": str(cred_to_update.id),
            "membership_id": str(tm.id),
            "tenant_id": tm.tenant.external_id,
            "tenant_name": tm.tenant.canonical_name,
        }
    )


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
