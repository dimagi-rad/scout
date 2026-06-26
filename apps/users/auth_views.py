"""Auth endpoints: csrf, me, login, logout, signup, providers, disconnect."""

import json
import logging

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import async_to_sync
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ValidationError as _ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.users.decorators import async_login_required, login_required_json
from apps.users.models import TenantConnection, TenantMembership
from apps.users.rate_limiting import check_rate_limit, record_attempt
from apps.users.services.credential_resolver import aget_social_token
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.users.services.token_refresh import get_token_url

logger = logging.getLogger(__name__)

UserModel = get_user_model()

# Short-lived cache for the /me onboarding computation (arch #254, finding 07#4).
# The SPA polls /me; without a guard each poll re-hit all three provider APIs
# (CommCare / Connect / OCS) for a token-bearing user with no persisted
# memberships. We cache the computed flag briefly so a poll loop doesn't
# re-resolve. We deliberately cache only the *complete* (True) result long, and
# the *incomplete* (False) result for a short window — long enough to throttle
# the poll storm, short enough that onboarding still completes promptly once the
# user connects a tenant.
_ME_ONBOARDING_TTL = 30  # seconds


def _me_onboarding_cache_key(user) -> str:
    return f"me_onboarding:{user.pk}"


def _user_response(user, *, onboarding_complete=False):
    """Build standard user JSON response dict."""
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.get_full_name(),
        "is_staff": user.is_staff,
        "onboarding_complete": onboarding_complete,
    }


async def _atry_resolve_provider(user, provider, resolve_fn, provider_name):
    """Best-effort lazy OAuth onboarding resolution for a provider.

    Returns ``True`` only when the resolver actually persisted at least one
    membership. A bare "token exists and the resolver didn't raise" is NOT
    onboarding completion: ``resolve_commcare_domains`` (and friends) can return
    ``[]`` without raising, which previously flapped ``onboarding_complete`` to
    ``True`` while the persisted state stayed incomplete (arch #254, 07#4). The
    caller derives the authoritative flag from the persisted membership state,
    not from this return value.
    """
    token_obj = await aget_social_token(user, provider)
    if not token_obj:
        return False
    try:
        resolved = await resolve_fn(user, token_obj.token)
    except Exception:
        logger.warning("Failed to resolve %s in me_view", provider_name, exc_info=True)
        return False
    # Treat a falsy / empty result as "resolved nothing" so the flag can't flap.
    return bool(resolved)


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

    ``onboarding_complete`` is always the *persisted* membership state, never a
    transient "the resolver ran" signal (arch #254, 07#4). The whole
    computation — including the expensive provider re-resolution — is cached for
    a short TTL so the SPA's /me poll doesn't re-hit all three provider APIs on
    every tick.
    """
    user = request._authenticated_user

    cache_key = _me_onboarding_cache_key(user)
    cached = await cache.aget(cache_key)
    if cached is not None:
        return JsonResponse(_user_response(user, onboarding_complete=cached))

    onboarding_complete = await _aonboarding_complete(user)

    # If the user just completed OAuth but tenant resolution hasn't run yet,
    # resolve now so onboarding can complete. Providers are tried independently —
    # a successful CommCare resolution must not skip Connect.
    if not onboarding_complete:
        await _atry_resolve_provider(user, "commcare", resolve_commcare_domains, "CommCare")
        await _atry_resolve_provider(
            user, "commcare_connect", resolve_connect_opportunities, "Connect"
        )
        await _atry_resolve_provider(user, "ocs", resolve_ocs_chatbots, "OCS")
        # Authoritative flag = persisted state after the resolution attempt. This
        # is True only if a provider actually created a connection-backed
        # membership, so the flag can't flap True for a token-but-no-tenant user.
        onboarding_complete = await _aonboarding_complete(user)

    await cache.aset(cache_key, onboarding_complete, _ME_ONBOARDING_TTL)
    return JsonResponse(_user_response(user, onboarding_complete=onboarding_complete))


@require_POST
def login_view(request):
    """Email/password login."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    email = body.get("email", "").strip()
    password = body.get("password", "")

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

    return JsonResponse(_user_response(user, onboarding_complete=onboarding_complete))


@require_POST
def logout_view(request):
    """Logout and clear session."""
    logout(request)
    return JsonResponse({"ok": True})


@require_POST
def signup_view(request):
    """Create a new account with email and password, then log in."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    email = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        return JsonResponse({"error": "Email and password are required"}, status=400)

    if check_rate_limit(email):
        return JsonResponse({"error": "Too many attempts. Try again later."}, status=429)

    try:
        validate_password(password)
    except _ValidationError as e:
        return JsonResponse({"error": "; ".join(e.messages)}, status=400)

    if UserModel.objects.filter(email=email).exists():
        return JsonResponse(
            {"error": "Unable to create account. If you already have an account, try logging in."},
            status=400,
        )

    try:
        user = UserModel.objects.create_user(email=email, password=password)
    except IntegrityError:
        return JsonResponse(
            {"error": "Unable to create account. If you already have an account, try logging in."},
            status=400,
        )

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    return JsonResponse(_user_response(user), status=201)


@require_POST
@login_required_json
def disconnect_provider_view(request, provider_id):
    """Revoke OAuth API token for a provider, keeping the SocialAccount for login."""
    # Find tokens for this provider — check both provider class id and provider_id
    tokens = SocialToken.objects.filter(account__user=request.user, account__provider=provider_id)
    if not tokens.exists():
        app_provider_ids = list(
            SocialApp.objects.filter(provider=provider_id).values_list("provider_id", flat=True)
        )
        if app_provider_ids:
            tokens = SocialToken.objects.filter(
                account__user=request.user, account__provider__in=app_provider_ids
            )
    if not tokens.exists():
        return JsonResponse({"error": "No active connection to disconnect"}, status=404)

    tokens.delete()

    # Remove the provider's OAuth connection and archive the chatbots it served
    # (their conversations/data are retained and restored if reconnected).
    oauth_conns = TenantConnection.objects.filter(
        user=request.user,
        provider=provider_id,
        credential_type=TenantConnection.OAUTH,
    )
    TenantMembership.objects.filter(connection__in=oauth_conns).update(
        archived_at=timezone.now(), connection=None
    )
    oauth_conns.delete()

    # Bust the cached /me onboarding flag so the change is reflected immediately
    # rather than after the TTL (arch #254, 07#4).
    cache.delete(_me_onboarding_cache_key(request.user))

    return JsonResponse({"status": "disconnected"})


@require_GET
def providers_view(request):
    """Return OAuth providers configured for this site, with connection status if authenticated."""
    from apps.users.services.token_refresh import (
        TokenRefreshError,
        refresh_oauth_token,
        token_needs_refresh,
    )

    current_site = Site.objects.get_current()
    apps = SocialApp.objects.filter(sites=current_site).order_by("provider")

    connected_providers = set()
    token_status = {}  # provider -> "connected" | "expired"
    if request.user.is_authenticated:
        connected_providers = set(
            SocialAccount.objects.filter(user=request.user).values_list("provider", flat=True)
        )
        # Check token validity for connected providers
        tokens = SocialToken.objects.filter(
            account__user=request.user,
        ).select_related("account", "app")
        for social_token in tokens:
            provider = social_token.account.provider
            if token_needs_refresh(social_token.expires_at):
                # Attempt refresh
                token_url = get_token_url(provider)
                if token_url and social_token.token_secret:
                    try:
                        async_to_sync(refresh_oauth_token)(social_token, token_url)
                        token_status[provider] = "connected"
                    except TokenRefreshError:
                        token_status[provider] = "expired"
                else:
                    token_status[provider] = "expired"
            else:
                token_status[provider] = "connected"

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
