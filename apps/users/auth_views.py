"""Auth endpoints: csrf, me, login, logout, signup, providers, disconnect, popup token exchange."""

import json
import logging
import secrets

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.password_validation import validate_password
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ValidationError as _ValidationError
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.users.decorators import login_required_json
from apps.users.models import TenantMembership
from apps.users.rate_limiting import check_rate_limit, record_attempt
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


# ---------------------------------------------------------------------------
# Popup OAuth token relay (for embed iframe authentication)
# ---------------------------------------------------------------------------

POPUP_TOKEN_CACHE_PREFIX = "popup_token:"
POPUP_TOKEN_MAX_AGE = 30  # seconds


def popup_complete_view(request):
    """Minimal page rendered after OAuth callback in the popup window.

    Generates a signed one-time token and sends it to the opener (the embed
    iframe) via postMessage so the iframe can exchange it for a session cookie
    with the correct SameSite attributes.
    """
    if not request.user.is_authenticated:
        # OAuth didn't complete — redirect to login
        from django.shortcuts import redirect

        return redirect(settings.LOGIN_URL or "/accounts/login/")

    nonce = secrets.token_urlsafe(16)
    signer = TimestampSigner()
    token = signer.sign(f"{request.user.id}:{nonce}")

    # Store nonce in cache for one-time validation
    cache.set(f"{POPUP_TOKEN_CACHE_PREFIX}{nonce}", str(request.user.id), POPUP_TOKEN_MAX_AGE)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scout — Login Complete</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; margin: 0; background: #f8fafc; color: #1e293b;
    text-align: center;
  }}
  .card {{ max-width: 20rem; }}
  h2 {{ font-size: 1.25rem; margin-bottom: 0.5rem; }}
  p {{ color: #64748b; font-size: 0.9rem; }}
</style>
</head>
<body>
<div class="card">
  <h2>Login successful</h2>
  <p id="status">Returning to the app...</p>
</div>
<script>
(function() {{
  var token = {json.dumps(token)};
  var sent = false;
  if (window.opener) {{
    try {{
      window.opener.postMessage(
        {{type: 'scout:auth-token', token: token}},
        window.location.origin
      );
      sent = true;
    }} catch (e) {{
      // opener may have been closed or cross-origin
    }}
  }}
  if (sent) {{
    setTimeout(function() {{ window.close(); }}, 1500);
  }} else {{
    document.getElementById('status').textContent = 'You can close this window.';
  }}
}})();
</script>
</body>
</html>"""
    return HttpResponse(html)


@csrf_exempt
@require_POST
def token_exchange_view(request):
    """Exchange a signed popup token for a session cookie.

    Called by the embed iframe after receiving the token via postMessage.
    Sets session and CSRF cookies with SameSite=None so they work in
    cross-site iframe contexts.
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    token = body.get("token", "")
    if not token:
        return JsonResponse({"error": "Token required"}, status=400)

    # Validate signature and expiry
    signer = TimestampSigner()
    try:
        payload = signer.unsign(token, max_age=POPUP_TOKEN_MAX_AGE)
    except SignatureExpired:
        return JsonResponse({"error": "Token expired"}, status=401)
    except BadSignature:
        return JsonResponse({"error": "Invalid token"}, status=401)

    # Parse user_id and nonce
    try:
        user_id_str, nonce = payload.rsplit(":", 1)
    except ValueError:
        return JsonResponse({"error": "Invalid token"}, status=401)

    # One-time use: check and consume nonce
    cache_key = f"{POPUP_TOKEN_CACHE_PREFIX}{nonce}"
    cached_user_id = cache.get(cache_key)
    if cached_user_id is None or cached_user_id != user_id_str:
        return JsonResponse({"error": "Token already used or invalid"}, status=401)
    cache.delete(cache_key)

    # Look up user and create session
    user = UserModel.objects.filter(id=user_id_str).first()
    if user is None or not user.is_active:
        return JsonResponse({"error": "Invalid token"}, status=401)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    onboarding_complete = TenantMembership.objects.filter(
        user=user, credential__isnull=False
    ).exists()

    response = JsonResponse(_user_response(user, onboarding_complete=onboarding_complete))

    # Set cookies with SameSite=None for cross-site iframe usage
    session_cookie = settings.SESSION_COOKIE_NAME
    response.set_cookie(
        session_cookie,
        request.session.session_key,
        max_age=settings.SESSION_COOKIE_AGE,
        path=settings.SESSION_COOKIE_PATH,
        domain=settings.SESSION_COOKIE_DOMAIN,
        secure=True,
        httponly=settings.SESSION_COOKIE_HTTPONLY,
        samesite="None",
    )
    response.set_cookie(
        settings.CSRF_COOKIE_NAME,
        get_token(request),
        max_age=settings.CSRF_COOKIE_AGE,
        path=settings.CSRF_COOKIE_PATH,
        domain=settings.CSRF_COOKIE_DOMAIN,
        secure=True,
        httponly=False,
        samesite="None",
    )

    return response
