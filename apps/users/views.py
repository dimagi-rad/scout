"""Tenant management views."""

from __future__ import annotations

import logging

from allauth.socialaccount.models import SocialToken
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Lower
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.common.commcare_servers import DEFAULT_SERVER, get_commcare_server
from apps.common.http import parse_json_object, string_field
from apps.users.adapters import encrypt_credential
from apps.users.decorators import async_login_required
from apps.users.models import Tenant, TenantConnection, TenantMembership, User
from apps.users.services.api_key_providers import (
    STRATEGIES,
    CredentialVerificationError,
)
from apps.users.services.credential_resolver import (
    _aresolve_oauth_credential,
    aconnection_status,
    aiter_fresh_access_tokens,
    aiter_social_tokens,
)
from apps.users.services.oauth_scope import account_scope, scope_account_ids
from apps.users.services.ocs_team import adetect_team_from_api_key
from apps.users.services.onboarding_cache import me_onboarding_cache_key
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.workspaces.models import Workspace

TENANT_REFRESH_TTL = 3600  # seconds (1 hour)
FORCED_REFRESH_FLOOR = 60  # seconds between user-requested refreshes that hit upstream

logger = logging.getLogger(__name__)


# provider -> resolver, for the lazy refresh tenant_list_view runs on poll.
_PROVIDER_RESOLVERS = {
    "commcare": resolve_commcare_domains,
    "commcare_connect": resolve_connect_opportunities,
    "ocs": resolve_ocs_chatbots,
}


async def _arefresh_all_identities(user, *, force: bool = False) -> None:
    """Refresh upstream memberships for every identity the user holds.

    The TTL is keyed per *identity*, not per provider. A user with two OCS teams
    holds one token per team, and a single ``tenant_refresh:<user>:ocs`` key meant
    the first team refreshed cheaply and then suppressed the second for an hour —
    so the second team's chatbots could stay missing indefinitely (#156).

    Each identity is independent: one team's failure must not stop the others, and
    a raise means "skip refresh" (never revoke on an inconclusive fetch), so the
    TTL is only written on success.

    ``force`` bypasses the per-identity TTL so a user can pick up an opportunity or
    domain they were just added to. A per-user floor keeps a spammed button from
    fanning out to every upstream API; inside it the call degrades to the TTL path.
    """
    if force and not await cache.aadd(
        f"tenant_refresh_floor:{user.id}", True, FORCED_REFRESH_FLOOR
    ):
        force = False
    for provider, resolve in _PROVIDER_RESOLVERS.items():
        for token_obj in await aiter_social_tokens(user, provider):
            cache_key = f"tenant_refresh:{user.id}:{provider}:{token_obj.account_id}"
            if not force and await cache.aget(cache_key):
                continue
            try:
                credential = await _aresolve_oauth_credential(
                    token_obj, provider, account_scope(token_obj.account)
                )
                await resolve(
                    user, credential["value"], social_account=token_obj.account, allow_replace=False
                )
            except Exception:
                logger.warning(
                    "Failed to refresh %s tenants for account %s",
                    provider,
                    token_obj.account_id,
                    exc_info=True,
                )
                continue
            await cache.aset(cache_key, True, TENANT_REFRESH_TTL)


# Wrap the persistence loop in sync_to_async so transaction.atomic() applies.
# Django doesn't yet expose async-native transaction support, so this is the
# sanctioned bridge for transactional ORM writes from async views.
@sync_to_async
def _persist_api_key_connection(
    user, provider, descriptors, encrypted, team_slug, team_name, server=DEFAULT_SERVER
):
    """Create one API-key connection and link every chatbot it discovered to it.

    A key belongs to one server, which is both its connection's scope (what access
    verification asks) and its tenants' server (what the loaders ask).
    """
    rows = []
    with transaction.atomic():
        conn = TenantConnection.objects.create(
            user=user,
            provider=provider,
            credential_type=TenantConnection.API_KEY,
            encrypted_credential=encrypted,
            scope_key=server,
            scope_label=get_commcare_server(server).label if server else "",
        )
        for desc in descriptors:
            tenant, _ = Tenant.objects.get_or_create(
                provider=provider,
                server=server,
                external_id=desc.external_id,
                defaults={"canonical_name": desc.canonical_name},
            )
            # all_objects: re-adding an API key after its memberships were archived
            # must reuse the tombstone, not create a duplicate (unique(user,tenant)).
            tm, _ = TenantMembership.all_objects.get_or_create(user=user, tenant=tenant)
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

    Refreshes each connected identity's upstream access (TTL-throttled) before
    returning, so a team the user authorised elsewhere shows up on the next poll.
    ``?refresh=1`` bypasses the TTL (rate-limited per user) for an explicit Refresh.
    """
    user = request._authenticated_user

    await _arefresh_all_identities(user, force=request.GET.get("refresh") == "1")

    memberships = []
    # Not Meta.ordering: -last_selected_at sorts never-selected (NULL) rows first (#357).
    async for tm in (
        TenantMembership.objects.filter(user=user, archived_at__isnull=True)
        .select_related("tenant")
        .order_by(Lower("tenant__canonical_name"), "id")
    ):
        memberships.append(
            {
                "id": str(tm.id),
                "provider": tm.tenant.provider,
                "server": tm.tenant.server,
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

    body, err = parse_json_object(request)
    if err:
        return err
    tenant_membership_id, err = string_field(body, "tenant_id")
    if err:
        return err

    try:
        tm = await TenantMembership.objects.select_related("tenant").aget(
            id=tenant_membership_id, user=user
        )
    except (TenantMembership.DoesNotExist, ValidationError):
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


def _credential_fields(body: dict, strategy) -> tuple[dict | None, JsonResponse | None]:
    """The request's credential ``fields``, or a 400 when a value the strategy will
    ``.strip()`` and send upstream is not a string."""
    fields = body.get("fields") or {}
    if not isinstance(fields, dict):
        return None, JsonResponse({"error": "fields must be an object."}, status=400)
    for form_field in strategy.form_fields:
        value = fields.get(form_field["key"])
        if value is not None and not isinstance(value, str):
            return None, JsonResponse(
                {"error": f"fields.{form_field['key']} must be a string."}, status=400
            )
    return fields, None


@require_http_methods(["GET", "POST"])
@async_login_required
async def tenant_credential_list_view(request):
    """GET  /api/auth/connections/ — list the user's connections, chatbots grouped
    POST /api/auth/connections/ — add a new API-key connection

    The GET is the surface a user reads to answer "which teams am I connected
    to, and is each one healthy?". To connect another, they follow the provider's
    `login_url` from `/api/auth/providers/` — allauth's `?process=connect`
    round-trip adds an identity for a team they don't hold yet and updates the
    one they do.
    """
    user = request._authenticated_user

    if request.method == "GET":
        results = []
        async for conn in (
            TenantConnection.objects.filter(user=user)
            .select_related("social_account")
            .order_by("-created_at")
        ):
            chatbots = []
            async for tm in (
                conn.memberships.filter(archived_at__isnull=True)
                .select_related("tenant")
                .order_by(Lower("tenant__canonical_name"), "id")
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
            is_oauth = conn.credential_type == TenantConnection.OAUTH
            results.append(
                {
                    "connection_id": str(conn.id),
                    "provider": conn.provider,
                    "credential_type": conn.credential_type,
                    # The scope this credential authorises, so the user can see
                    # which teams they hold and which one is unhealthy — the
                    # self-remediation half of #156. Falls back to the label
                    # derived from a chatbot for connections predating the field.
                    "scope_key": conn.scope_key,
                    "scope_label": conn.scope_label,
                    # Per connection, not per provider: two teams have independent
                    # tokens and one can expire while the other is fine.
                    "status": await aconnection_status(conn) if is_oauth else None,
                    "chatbots": chatbots,
                }
            )
        return JsonResponse(results, safe=False)

    body, err = parse_json_object(request)
    if err:
        return err

    provider, err = string_field(body, "provider")
    if err:
        return err
    provider = provider.strip()

    strategy = STRATEGIES.get(provider)
    if strategy is None:
        return JsonResponse({"error": f"Unknown provider '{provider}'"}, status=400)

    fields, err = _credential_fields(body, strategy)
    if err:
        return err

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
        server = strategy.server_for(fields)
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
            user, provider, descriptors, encrypted, team_slug, team_name, server
        )
    except Exception as e:
        logger.exception("Failed to persist connection for provider %s", provider)
        return JsonResponse({"error": str(e)}, status=500)

    await cache.adelete(me_onboarding_cache_key(user))
    return JsonResponse({"memberships": memberships_payload}, status=201)


@sync_to_async
def _archive_and_delete_connection(conn):
    """Archive the connection's live memberships (retaining data), then delete it.

    For an OAuth connection this deletes every token for its scope, so
    disconnecting one OCS team leaves the user's other teams connected — the
    provider-wide `disconnect_provider_view` is the "sign out of everything"
    action. The SocialAccount survives either way: it is a login identity, not a
    data credential.
    """
    with transaction.atomic():
        # Serialize scope creation/replacement against disconnect, including absent rows.
        User.objects.select_for_update().get(pk=conn.user_id)
        if conn.credential_type == TenantConnection.OAUTH:
            SocialToken.objects.filter(
                account_id__in=scope_account_ids(conn.user_id, conn.provider, conn.scope_key)
            ).delete()
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
        await cache.adelete(me_onboarding_cache_key(user))
        return JsonResponse({"status": "removed"})

    body, err = parse_json_object(request)
    if err:
        return err

    strategy = STRATEGIES.get(conn.provider)
    if strategy is None:
        return JsonResponse(
            {"error": f"Provider '{conn.provider}' has no API-key strategy"},
            status=400,
        )

    fields, err = _credential_fields(body, strategy)
    if err:
        return err

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
        # The server is fixed at creation; a rotated key is checked against the same one.
        await strategy.verify_for_tenant(
            {**fields, "server": sample.tenant.server}, external_id=sample.tenant.external_id
        )
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

    body, err = parse_json_object(request)
    if err:
        return err

    provider, err = string_field(body, "provider")
    if err:
        return err
    tenant_id, err = string_field(body, "tenant_id")
    if err:
        return err
    server, err = string_field(body, "server", DEFAULT_SERVER)
    if err:
        return err
    provider, tenant_id, server = provider.strip(), tenant_id.strip(), server.strip()

    if not provider or not tenant_id:
        return JsonResponse({"error": "provider and tenant_id are required"}, status=400)

    try:
        tm = await TenantMembership.objects.select_related("tenant").aget(
            user=user,
            tenant__provider=provider,
            tenant__server=server,
            tenant__external_id=tenant_id,
        )
    except TenantMembership.DoesNotExist:
        if provider == "commcare_connect" and not server:
            credentials = await aiter_fresh_access_tokens(user, "commcare_connect")
            if not credentials:
                return JsonResponse(
                    {"error": "No Connect OAuth token. Please log in with Connect first."},
                    status=404,
                )

            account, access_token = credentials[0]
            # Resolve the user's actual opportunities from the Connect API
            # to verify they have access to the requested tenant_id.
            memberships = await resolve_connect_opportunities(
                user, access_token, social_account=account, allow_replace=False
            )
            tm = (
                await TenantMembership.objects.select_related("tenant")
                .filter(pk__in=[m.pk for m in memberships], tenant__external_id=tenant_id)
                .afirst()
            )
            if tm is None:
                return JsonResponse(
                    {"error": "Opportunity not found for this user"},
                    status=404,
                )
        else:
            return JsonResponse({"error": "Tenant not found"}, status=404)

    tm.last_selected_at = timezone.now()
    await tm.asave(update_fields=["last_selected_at"])

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
