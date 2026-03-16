"""Auth endpoints: csrf, me, login, logout, signup, providers, disconnect."""

import json
import logging

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError as _ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.chat.helpers import login_required_json
from apps.chat.rate_limiting import check_rate_limit, record_attempt
from apps.users.models import TenantMembership
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
)
from apps.users.views import _get_commcare_token, _get_connect_token

logger = logging.getLogger(__name__)

UserModel = get_user_model()

PROVIDER_DISPLAY = {
    "google": "Google",
    "github": "GitHub",
    "commcare": "CommCare",
    "commcare_connect": "CommCare Connect",
}

PROVIDER_TOKEN_URLS = {
    "commcare": "https://www.commcarehq.org/oauth/token/",
    "commcare_connect": "https://connect.commcarehq.org/oauth/token/",
}


def _user_response(user, *, onboarding_complete=False):
    """Build standard user JSON response dict."""
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.get_full_name(),
        "is_staff": user.is_staff,
        "onboarding_complete": onboarding_complete,
    }


def _try_resolve_provider(user, get_token_fn, resolve_fn, provider_name):
    """Attempt lazy OAuth onboarding resolution for a provider."""
    token = get_token_fn(user)
    if not token:
        return False
    try:
        resolve_fn(user, token)
        return True
    except Exception:
        logger.warning("Failed to resolve %s in me_view", provider_name, exc_info=True)
        return False


@ensure_csrf_cookie
@require_GET
def csrf_view(request):
    """Return CSRF cookie so the SPA can read it."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_GET
@login_required_json
def me_view(request):
    """Return current user info or 401."""
    user = request.user

    onboarding_complete = TenantMembership.objects.filter(
        user=user,
        credential__isnull=False,
    ).exists()

    # If the user just completed CommCare OAuth but tenant resolution hasn't
    # run yet, resolve now so onboarding can complete.
    if not onboarding_complete:
        onboarding_complete = _try_resolve_provider(
            user, _get_commcare_token, resolve_commcare_domains, "CommCare"
        )

    # Same for Connect OAuth — resolve opportunities if token exists.
    if not onboarding_complete:
        onboarding_complete = _try_resolve_provider(
            user, _get_connect_token, resolve_connect_opportunities, "Connect"
        )

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
        credential__isnull=False,
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
    return JsonResponse({"status": "disconnected"})


@login_required_json
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

    connected_providers = set(
        SocialAccount.objects.filter(user=request.user).values_list("provider", flat=True)
    )
    # Check token validity for connected providers
    token_status = {}  # provider -> "connected" | "expired"
    tokens = SocialToken.objects.filter(
        account__user=request.user,
    ).select_related("account", "app")
    for social_token in tokens:
        provider = social_token.account.provider
        if token_needs_refresh(social_token.expires_at):
            # Attempt refresh
            token_url = PROVIDER_TOKEN_URLS.get(provider)
            if token_url and social_token.token_secret:
                try:
                    refresh_oauth_token(social_token, token_url)
                    token_status[provider] = "connected"
                except TokenRefreshError:
                    token_status[provider] = "expired"
            else:
                token_status[provider] = "expired"
        else:
            token_status[provider] = "connected"

    providers = []
    for app in apps:
        # SocialAccount.provider stores the provider_id (e.g. "commcare_prod"),
        # not the provider class id (e.g. "commcare"), so check both.
        is_connected = app.provider in connected_providers or app.provider_id in connected_providers
        entry = {
            "id": app.provider,
            "name": PROVIDER_DISPLAY.get(app.provider, app.name),
            "login_url": f"/accounts/{app.provider}/login/",
            "connected": is_connected,
        }
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
