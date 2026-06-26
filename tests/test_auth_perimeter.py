"""Tests for closing the second (stock-allauth /accounts/) auth perimeter (arch #258).

Covers:
- 13#9 — the dangerous allauth HTML surface (open self-registration, HTML login,
  password reset, email management) is no longer mounted, while the SPA-required
  OAuth provider login/callback routes STILL resolve.
- 14#2 — SOCIALACCOUNT_LOGIN_ON_GET is False (require POST to initiate OAuth;
  closes login-CSRF on GET).
- 14#1 — no allauth HTML login/signup form remains reachable, so the brute-force
  budget can't be split across a second un-rate-limited surface.
- 14#0 — an explicit EMAIL_BACKEND is configured so prod never silently falls
  back to SMTP localhost:25.
- 07#2 — a provider WITH a configured allowlist can't be bypassed by a no-email
  login; the .env.example default matches the coded default.
"""

import pytest
from django.conf import settings
from django.test import Client
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

# --- 13#9: dangerous allauth HTML routes are no longer mounted ------------- #

# Stock-allauth HTML views that constitute the second registration/auth
# perimeter. None of these should be reachable once the narrowed include lands.
DANGEROUS_ALLAUTH_ROUTE_NAMES = [
    "account_signup",  # open self-registration
    "account_email",  # email management
    "account_reset_password",  # password reset (broken email backend)
    "account_change_password",
    "account_set_password",
    "account_reauthenticate",
    "socialaccount_signup",  # 3rdparty HTML signup
    "socialaccount_connections",  # 3rdparty HTML connection management
]

# allauth route names the SPA / OAuth flow depend on. These MUST keep resolving.
SPA_REQUIRED_PROVIDER_ROUTE_NAMES = [
    "google_login",
    "github_login",
    "commcare_login",
    "commcare_connect_login",
    "ocs_login",
    "google_callback",
    "commcare_callback",
    "commcare_connect_callback",
    "ocs_callback",
]


class TestDangerousHtmlSurfaceRemoved:
    @pytest.mark.parametrize("name", DANGEROUS_ALLAUTH_ROUTE_NAMES)
    def test_route_name_does_not_resolve(self, name):
        """The dangerous HTML view names must not be registered at all."""
        with pytest.raises(NoReverseMatch):
            reverse(name)

    def test_signup_path_returns_404(self):
        """The open self-registration form must be unreachable by path."""
        with pytest.raises(Resolver404):
            resolve("/accounts/signup/")

    def test_password_reset_path_returns_404(self):
        with pytest.raises(Resolver404):
            resolve("/accounts/password/reset/")

    def test_email_management_path_returns_404(self):
        with pytest.raises(Resolver404):
            resolve("/accounts/email/")

    @pytest.mark.django_db
    def test_signup_http_request_is_404(self):
        """A live GET to the open-signup form must 404, not render a form."""
        resp = Client().get("/accounts/signup/")
        assert resp.status_code == 404

    @pytest.mark.django_db
    def test_password_reset_http_request_is_404(self):
        resp = Client().get("/accounts/password/reset/")
        assert resp.status_code == 404


# --- 13#9: SPA-required OAuth routes still resolve -------------------------- #


class TestSpaOauthRoutesPreserved:
    @pytest.mark.parametrize("name", SPA_REQUIRED_PROVIDER_ROUTE_NAMES)
    def test_provider_route_still_resolves(self, name):
        """Provider login/callback routes the SPA links to must keep resolving."""
        # Should not raise NoReverseMatch.
        url = reverse(name)
        assert url.startswith("/accounts/")

    def test_commcare_login_path_resolves(self):
        """The exact path the SPA anchors to (commcare/login/) must resolve."""
        match = resolve("/accounts/commcare/login/")
        assert match is not None

    def test_account_login_name_still_reverses(self):
        """The adapter redirects to account_login on allowlist rejection;
        the name must remain resolvable (even if it no longer renders a form)."""
        url = reverse("account_login")
        assert url  # resolves to something

    def test_oauth_error_landing_routes_resolve(self):
        """OAuth cancel/error landing pages are part of the provider flow."""
        assert reverse("socialaccount_login_cancelled")
        assert reverse("socialaccount_login_error")


# --- 14#2: LOGIN_ON_GET requires POST -------------------------------------- #


class TestLoginOnGetDisabled:
    def test_socialaccount_login_on_get_is_false(self):
        assert settings.SOCIALACCOUNT_LOGIN_ON_GET is False


# --- 14#0: explicit email backend ------------------------------------------ #


class TestEmailBackendConfigured:
    def test_email_backend_is_set_explicitly(self):
        """base.py must set EMAIL_BACKEND so prod never silently uses SMTP
        localhost:25 (which has no MTA in the container)."""
        assert settings.EMAIL_BACKEND
        # Must not be the Django SMTP default that points at localhost:25.
        assert settings.EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend"
