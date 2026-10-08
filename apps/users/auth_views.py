"""Auth endpoints: csrf, me, login, logout, providers, disconnect."""

import logging

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.agents.model_label import model_display_name
from apps.common.commcare_servers import (
    UnknownCommCareServer,
    get_commcare_server,
    server_for_provider,
)
from apps.common.http import parse_json_object, string_field
from apps.telemetry.models import EventKind
from apps.telemetry.recorder import arecord
from apps.users.decorators import async_login_required, login_required_json
from apps.users.models import (
    SCOPED_OAUTH_PROVIDERS,
    TenantConnection,
    TenantMembership,
    User,
)
from apps.users.rate_limiting import check_rate_limit, record_attempt
from apps.users.services.credential_resolver import aiter_fresh_access_tokens
from apps.users.services.oauth_scope import (
    account_scope,
    canonical_provider,
    is_active_identity,
    ocs_scope_unusable,
    provider_accounts,
    scope_account_ids,
)
from apps.users.services.onboarding_cache import ME_ONBOARDING_TTL, me_onboarding_cache_key
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.users.services.token_refresh import (
    INTERACTIVE_DB_DEADLINE,
    TokenRefreshError,
    TokenRefreshUnavailable,
    credential_fingerprint,
    get_token_url,
    refresh_oauth_token,
    token_health,
    token_needs_refresh,
)
from apps.workspaces.access import aresolve_workspace_access_ex
from apps.workspaces.models import WorkspaceMembership

logger = logging.getLogger(__name__)


def _last_workspace_id(user) -> str | None:
    """The remembered workspace id, only while the user is still a member of it."""
    if user.last_workspace_id is None:
        return None
    # authz-exempt: a hint the client re-checks against its own workspace list.
    is_member = WorkspaceMembership.objects.filter(
        user=user, workspace_id=user.last_workspace_id
    ).exists()
    return str(user.last_workspace_id) if is_member else None


async def _alast_workspace_id(user) -> str | None:
    if user.last_workspace_id is None:
        return None
    # authz-exempt: a hint the client re-checks against its own workspace list.
    is_member = await WorkspaceMembership.objects.filter(
        user=user, workspace_id=user.last_workspace_id
    ).aexists()
    return str(user.last_workspace_id) if is_member else None


def _user_response(user, *, onboarding_complete=False, last_workspace_id=None):
    """Build standard user JSON response dict."""
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.get_full_name(),
        "is_staff": user.is_staff,
        "onboarding_complete": onboarding_complete,
        "last_workspace_id": last_workspace_id,
        "agent_model": {
            "id": settings.DEFAULT_LLM_MODEL,
            "label": model_display_name(settings.DEFAULT_LLM_MODEL),
        },
    }


async def _atry_onboarding_resolve_provider(user, provider, resolve_fn, provider_name):
    """Best-effort lazy OAuth onboarding resolution for a provider.

    Only ``me_view`` calls it, and only before onboarding completes; it is not
    a way to revalidate access for an onboarded user.

    Returns ``True`` only when the resolver actually persisted at least one
    membership. A bare "token exists and the resolver didn't raise" is NOT
    onboarding completion: ``resolve_commcare_domains`` (and friends) can return
    ``[]`` without raising, which previously flapped ``onboarding_complete`` to
    ``True`` while the persisted state stayed incomplete (arch #254, 07#4). The
    caller derives the authoritative flag from the persisted membership state,
    not from this return value.

    Every identity the user holds for the provider is resolved, not just one: an
    OCS token is team-scoped, so stopping at the first would leave a second
    team's chatbots undiscovered (#156). One team failing must not skip the rest.
    """
    resolved_any = False
    for account, access_token in await aiter_fresh_access_tokens(user, provider):
        try:
            resolved = await resolve_fn(
                user, access_token, social_account=account, allow_replace=False
            )
        except Exception:
            logger.warning("Failed to resolve %s in me_view", provider_name, exc_info=True)
            continue
        resolved_any = resolved_any or bool(resolved)
    return resolved_any  # falsy/empty = "resolved nothing" so the flag can't flap


async def _aonboarding_complete(user) -> bool:
    """True when the user has at least one active, connection-backed membership."""
    return await TenantMembership.objects.filter(
        user=user,
        connection__isnull=False,
        archived_at__isnull=True,
    ).aexists()


@ensure_csrf_cookie
@require_GET
def csrf_view(request):
    """Return CSRF cookie so the SPA can read it."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_GET
@async_login_required
async def me_view(request):
    """Return current user info or 401.

    Also a lazy *onboarding* hook, not a revalidation path: while the user has no
    connection-backed membership, it resolves their provider tenants inline so
    onboarding can finish. Once onboarded it never calls a provider, so it does
    not refresh or re-check upstream access; that is the freshness gate's job
    (``UPSTREAM_ACCESS_FRESHNESS_ENFORCED``).

    ``onboarding_complete`` is always the *persisted* membership state, never a
    transient "the resolver ran" signal (arch #254, 07#4). The whole
    computation — including the expensive provider re-resolution — is cached for
    a short TTL so the SPA's /me poll doesn't re-hit all three provider APIs on
    every tick.
    """
    user = request._authenticated_user

    last_workspace_id = await _alast_workspace_id(user)
    cache_key = me_onboarding_cache_key(user)
    cached = await cache.aget(cache_key)
    if cached is not None:
        return JsonResponse(
            _user_response(user, onboarding_complete=cached, last_workspace_id=last_workspace_id)
        )

    onboarding_complete = await _aonboarding_complete(user)

    # Onboarding only: if the user just completed OAuth but tenant resolution
    # hasn't run yet, resolve now so onboarding can complete. Providers are tried independently —
    # a successful CommCare resolution must not skip Connect.
    if not onboarding_complete:
        await _atry_onboarding_resolve_provider(
            user, "commcare", resolve_commcare_domains, "CommCare"
        )
        await _atry_onboarding_resolve_provider(
            user, "commcare_connect", resolve_connect_opportunities, "Connect"
        )
        await _atry_onboarding_resolve_provider(user, "ocs", resolve_ocs_chatbots, "OCS")
        # Authoritative flag = persisted state after the resolution attempt. This
        # is True only if a provider actually created a connection-backed
        # membership, so the flag can't flap True for a token-but-no-tenant user.
        onboarding_complete = await _aonboarding_complete(user)

    await cache.aset(cache_key, onboarding_complete, ME_ONBOARDING_TTL)
    return JsonResponse(
        _user_response(
            user,
            onboarding_complete=onboarding_complete,
            last_workspace_id=last_workspace_id,
        )
    )


@require_POST
def login_view(request):
    """Email/password login."""
    body, err = parse_json_object(request)
    if err:
        return err

    email, err = string_field(body, "email")
    if err:
        return err
    password, err = string_field(body, "password")
    if err:
        return err
    email = email.strip()

    if not email or not password:
        return JsonResponse({"error": "Email and password are required"}, status=400)

    if check_rate_limit(email):
        return JsonResponse({"error": "Too many attempts. Try again later."}, status=429)

    user = authenticate(request, username=email, password=password)
    if user is None or not user.is_active:
        record_attempt(email, False)
        return JsonResponse({"error": "Invalid credentials"}, status=401)

    record_attempt(email, True)
    login(request, user)

    onboarding_complete = TenantMembership.objects.filter(
        user=user,
        connection__isnull=False,
        archived_at__isnull=True,
    ).exists()

    return JsonResponse(
        _user_response(
            user,
            onboarding_complete=onboarding_complete,
            last_workspace_id=_last_workspace_id(user),
        )
    )


@require_POST
@async_login_required
async def last_workspace_view(request):
    """Remember the workspace the user is in, so the next visit opens it."""
    body, err = parse_json_object(request)
    if err:
        return err
    workspace_id, err = string_field(body, "workspace_id")
    if err:
        return err
    user = request._authenticated_user
    try:
        allowed = (
            await aresolve_workspace_access_ex(user, workspace_id, verification=None)
        ).granted
    except (ValidationError, ValueError):
        allowed = False
    if not allowed:
        return JsonResponse({"error": "Workspace not found"}, status=404)
    switched = (
        await User.objects.filter(pk=user.pk)
        .exclude(last_workspace_id=workspace_id)
        .aupdate(last_workspace_id=workspace_id)
    )
    if switched:
        await arecord(EventKind.WORKSPACE_SWITCH, user_id=user.pk, workspace_id=workspace_id)
    return JsonResponse({"ok": True})


@require_POST
def logout_view(request):
    """Logout and clear session."""
    logout(request)
    return JsonResponse({"ok": True})


@require_POST
@login_required_json
def disconnect_provider_view(request, provider_id):
    """Revoke every OAuth token for a provider, keeping the SocialAccounts for login.

    Provider-wide by design: for a scoped provider this signs the user out of
    *all* their teams. Removing a single team is
    ``DELETE /api/auth/connections/<id>/``. Each CommCare HQ server is its own
    provider, so disconnecting one leaves the other signed in.
    """
    # Data providers may use configured allauth IDs such as commcare_prod.
    provider = canonical_provider(provider_id)
    if provider in ("commcare", "commcare_connect", "ocs"):
        tokens = SocialToken.objects.filter(
            account__in=provider_accounts(request.user.pk, provider)
        )
    else:
        tokens = SocialToken.objects.filter(
            account__user=request.user, account__provider=provider_id
        )
        if not tokens.exists():
            app_provider_ids = list(
                SocialApp.objects.filter(provider=provider_id).values_list("provider_id", flat=True)
            )
            tokens = SocialToken.objects.filter(
                account__user=request.user, account__provider__in=app_provider_ids
            )
    configured_ids = (
        SocialApp.objects.filter(provider=provider)
        .exclude(provider_id="")
        .values_list("provider_id", flat=True)
    )
    tokens = tokens | SocialToken.objects.filter(
        account__user=request.user, account__provider__in=configured_ids
    )
    oauth_conns = TenantConnection.objects.filter(
        user=request.user,
        provider=provider,
        credential_type=TenantConnection.OAUTH,
    )
    if provider == "commcare":
        # Each HQ server is its own sign-in: disconnecting EU must leave www connected.
        # www may be configured under an alias id (hq_production) that only the
        # union above finds, so www keeps it and drops the other servers' identities.
        server = server_for_provider(provider_id)
        if server:
            tokens = SocialToken.objects.filter(
                account_id__in=scope_account_ids(request.user.pk, provider, server)
            )
        else:
            tokens = tokens.exclude(
                account_id__in=[
                    account.pk
                    for account in provider_accounts(request.user.pk, provider)
                    if account_scope(account)
                ]
            )
        oauth_conns = oauth_conns.filter(scope_key=server)
    if not tokens.exists():
        return JsonResponse({"error": "No active connection to disconnect"}, status=404)

    tokens.delete()

    # Remove the provider's OAuth connection and archive the chatbots it served
    # (their conversations/data are retained and restored if reconnected).
    TenantMembership.objects.filter(connection__in=oauth_conns).update(
        archived_at=timezone.now(),
        archived_reason=TenantMembership.ARCHIVED_DISCONNECTED,
        connection=None,
    )
    oauth_conns.delete()

    # Bust the cached /me onboarding flag so the change is reflected immediately
    # rather than after the TTL (arch #254, 07#4).
    cache.delete(me_onboarding_cache_key(request.user))

    return JsonResponse({"status": "disconnected"})


def _record_status(seen: dict[str, set[str]], provider: str, status: str) -> None:
    seen.setdefault(provider, set()).add(status)


@require_GET
def providers_view(request):
    """Return OAuth providers configured for this site, with connection status if authenticated."""

    current_site = Site.objects.get_current()
    apps = SocialApp.objects.filter(sites=current_site).order_by("provider")

    connected_providers = set()
    token_status = {}  # provider -> "connected" | "expired" | "unavailable" | "needs_team"
    connection_ids_by_account_provider: dict[str, list[str]] = {}
    if request.user.is_authenticated:
        connected_providers = set(
            SocialAccount.objects.filter(user=request.user).values_list("provider", flat=True)
        )
        tokens = SocialToken.objects.filter(
            account__user=request.user,
        ).select_related("account", "app")
        # provider -> every one of its identities' statuses. A scoped provider now
        # has one token per team (#156), and the old per-provider assignment was
        # last-row-wins, so a healthy team could be reported as expired purely on
        # queryset order. Reduced below to "connected while at least one works",
        # then "expired" (the only status naming a user action) ahead of "unavailable";
        # the per-team detail lives on /api/auth/connections/.
        oauth_connections = list(
            TenantConnection.objects.filter(
                user=request.user, credential_type=TenantConnection.OAUTH
            ).select_related("social_account")
        )
        bindings = {
            (conn.provider, conn.scope_key): conn.social_account_id for conn in oauth_connections
        }
        # Which connections each card covers, so the page can show their access
        # problems on it. The card is keyed by the identity's provider, not the
        # connection's: both CommCare HQ servers' connections are "commcare".
        for conn in oauth_connections:
            # A legacy or orphaned connection has no identity; its own provider
            # still puts it on a card, so its notice can offer Reconnect.
            if conn.social_account:
                key = conn.social_account.provider
            elif conn.provider == "commcare" and conn.scope_key:
                try:
                    key = get_commcare_server(conn.scope_key).provider_id
                except UnknownCommCareServer:
                    key = conn.provider
            else:
                key = conn.provider
            connection_ids_by_account_provider.setdefault(key, []).append(str(conn.id))
        seen_statuses: dict[str, set[str]] = {}
        for social_token in tokens:
            if not is_active_identity(social_token.account, bindings):
                continue
            provider = social_token.account.provider
            # Not a healthy connection: it can reach no data sources (#379).
            if ocs_scope_unusable(provider, account_scope(social_token.account)):
                _record_status(seen_statuses, provider, "needs_team")
                continue
            scope_key = account_scope(social_token.account)
            token_url = get_token_url(provider, scope_key)
            can_refresh = bool(token_url and social_token.token_secret and social_token.app)
            refresh_failed = False
            refresh_unavailable = False
            if can_refresh and token_needs_refresh(social_token.expires_at):
                try:
                    async_to_sync(refresh_oauth_token)(
                        social_token, token_url, db_timeout=INTERACTIVE_DB_DEADLINE
                    )
                except TokenRefreshUnavailable:
                    # The stored credential isn't known to be dead (provider blip, Scout's
                    # own invalid_client, contended DB), so reconnecting can't help (#779).
                    refresh_unavailable = True
                except TokenRefreshError:
                    refresh_failed = True
            refresh_failed = (
                refresh_failed
                or TenantConnection.objects.filter(
                    social_account_id=social_token.account_id,
                    oauth_refresh_failure_fingerprint=credential_fingerprint(social_token),
                ).exists()
            )
            status = token_health(
                social_token, provider, scope_key=scope_key, refresh_failed=refresh_failed
            )
            _record_status(
                seen_statuses,
                provider,
                "unavailable" if refresh_unavailable and status == "connected" else status,
            )
        token_status = {
            provider: next(
                (s for s in ("connected", "expired", "unavailable", "needs_team") if s in statuses),
                "expired",
            )
            for provider, statuses in seen_statuses.items()
        }

    providers = []
    for app in apps:
        entry = {
            "id": app.provider,
            "name": app.name,
            # No prefix — the frontend prepends BASE_PATH to all API-provided URLs
            "login_url": f"/accounts/{app.provider}/login/",
        }
        if request.user.is_authenticated:
            # SocialAccount.provider stores the provider_id (e.g. "commcare_prod"),
            # not the provider class id (e.g. "commcare"), so check both.
            is_connected = (
                app.provider in connected_providers or app.provider_id in connected_providers
            )
            entry["connected"] = is_connected
            # Lets the UI offer "connect another team" rather than only
            # connect/disconnect, for a provider whose token covers one scope.
            entry["supports_multiple_scopes"] = (
                app.provider in SCOPED_OAUTH_PROVIDERS or app.provider_id in SCOPED_OAUTH_PROVIDERS
            )
            entry["connection_ids"] = sorted(
                {
                    *connection_ids_by_account_provider.get(app.provider, []),
                    *connection_ids_by_account_provider.get(app.provider_id, []),
                }
            )
            if is_connected:
                # No token_status entry means the SocialAccount exists but no token
                # (user revoked API access) — treat as disconnected
                entry["status"] = token_status.get(
                    app.provider, token_status.get(app.provider_id, "disconnected")
                )
            else:
                entry["status"] = None
        providers.append(entry)

    return JsonResponse({"providers": providers})
