"""Auth endpoints: csrf, me, login, logout, signup, providers, disconnect."""

import json

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib.auth import authenticate, login, logout
from django.contrib.sites.models import Site
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.chat.rate_limiting import check_rate_limit, record_attempt


@ensure_csrf_cookie
@require_GET
def csrf_view(request):
    """Return CSRF cookie so the SPA can read it."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_GET
def me_view(request):
    """Return current user info or 401."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Not authenticated"}, status=401)
    user = request.user

    from apps.users.models import TenantMembership

    onboarding_complete = TenantMembership.objects.filter(
        user=user,
        credential__isnull=False,
    ).exists()

    # If the user just completed CommCare OAuth but tenant resolution hasn't
    # run yet, resolve now so onboarding can complete.
    if not onboarding_complete:
        from apps.users.views import _get_commcare_token

        access_token = _get_commcare_token(user)
        if access_token:
            try:
                from apps.users.services.tenant_resolution import resolve_commcare_domains

                resolve_commcare_domains(user, access_token)
                onboarding_complete = True
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to resolve CommCare domains in me_view", exc_info=True
                )

    # Same for Connect OAuth — resolve opportunities if token exists.
    if not onboarding_complete:
        from apps.users.views import _get_connect_token

        connect_token = _get_connect_token(user)
        if connect_token:
            try:
                from apps.users.services.tenant_resolution import resolve_connect_opportunities

                resolve_connect_opportunities(user, connect_token)
                onboarding_complete = True
            except Exception:
                import logging

                logging.getLogger(__name__).warning(
                    "Failed to resolve Connect opportunities in me_view", exc_info=True
                )

    return JsonResponse(
        {
            "id": str(user.id),
            "email": user.email,
            "name": user.get_full_name(),
            "is_staff": user.is_staff,
            "onboarding_complete": onboarding_complete,
        }
    )


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

    from apps.users.models import TenantMembership

    onboarding_complete = TenantMembership.objects.filter(
        user=user,
        credential__isnull=False,
    ).exists()

    return JsonResponse(
        {
            "id": str(user.id),
            "email": user.email,
            "name": user.get_full_name(),
            "is_staff": user.is_staff,
            "onboarding_complete": onboarding_complete,
        }
    )


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

    from django.contrib.auth import get_user_model

    UserModel = get_user_model()

    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError as _ValidationError
    from django.db import IntegrityError

    try:
        validate_password(password)
    except _ValidationError as e:
        return JsonResponse({"error": "; ".join(e.messages)}, status=400)

    if UserModel.objects.filter(email=email).exists():
        return JsonResponse({"error": "An account with this email already exists"}, status=400)

    try:
        user = UserModel.objects.create_user(email=email, password=password)
    except IntegrityError:
        return JsonResponse({"error": "An account with this email already exists"}, status=400)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    return JsonResponse(
        {
            "id": str(user.id),
            "email": user.email,
            "name": user.get_full_name(),
            "is_staff": user.is_staff,
        },
        status=201,
    )


PROVIDER_DISPLAY = {
    "google": "Google",
    "github": "GitHub",
    "commcare": "CommCare",
    "commcare_connect": "CommCare Connect",
}


@require_POST
def disconnect_provider_view(request, provider_id):
    """Revoke OAuth API token for a provider, keeping the SocialAccount for login."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Not authenticated"}, status=401)

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


@require_GET
def providers_view(request):
    """Return OAuth providers configured for this site, with connection status if authenticated."""
    from apps.users.services.token_refresh import (
        TokenRefreshError,
        refresh_oauth_token,
        token_needs_refresh,
    )

    # Map provider IDs to their token endpoint URLs for refresh
    PROVIDER_TOKEN_URLS = {
        "commcare": "https://www.commcarehq.org/oauth/token/",
        "commcare_connect": "https://connect.commcarehq.org/oauth/token/",
    }

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
        entry = {
            "id": app.provider,
            "name": PROVIDER_DISPLAY.get(app.provider, app.name),
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
