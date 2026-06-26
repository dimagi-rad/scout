"""Narrowed allauth URL surface (arch #258, finding 13#9).

Scout's SPA owns the human-facing auth UI: email/password login + signup live at
``/api/auth/`` (rate-limited, CSRF-protected) and social login is initiated from
React via the provider login URLs returned by ``/api/auth/providers/``.

Stock allauth (``include('allauth.urls')``) additionally mounts a *second*,
ungoverned HTML auth perimeter parallel to the SPA: open self-registration
(``/accounts/signup/``), an HTML login form, password reset, email management,
and the ``3rdparty/`` HTML connection views. None of those are surfaced by the
SPA, none are covered by Scout's per-email rate limiter, and password
reset/email verification can't deliver in production (no MTA — see 14#0). They
are pure attack surface.

This module mounts ONLY the routes the SPA / OAuth round-trip actually needs:

* per-provider ``<provider>/login/`` and ``<provider>/login/callback/`` routes
  (built by ``build_provider_urlpatterns``) — the SPA links to these,
* the OAuth ``login/cancelled/`` and ``login/error/`` landing pages,
* an ``account_login`` *name* that redirects to the SPA root, so allauth's
  ``LOGIN_URL`` default and the adapter's allowlist-rejection redirect
  (``redirect("account_login")``) still resolve without rendering an HTML form.

It deliberately does NOT include ``allauth.account.urls`` or the
``allauth.socialaccount.urls`` (``3rdparty/``) HTML views.
"""

from allauth.socialaccount.views import login_cancelled, login_error
from allauth.urls import build_provider_urlpatterns
from django.urls import path
from django.views.generic.base import RedirectView

# Note: allauth's LOGIN_REDIRECT_URL/LOGIN_URL and our adapter both reference the
# "account_login" view name. We keep the *name* resolvable but point it at the
# SPA root rather than the stock HTML login form. The SPA renders its own login
# UI and surfaces any queued allauth messages on the next page load.
urlpatterns = [
    path("login/", RedirectView.as_view(url="/", query_string=True), name="account_login"),
    path("login/cancelled/", login_cancelled, name="socialaccount_login_cancelled"),
    path("login/error/", login_error, name="socialaccount_login_error"),
    *build_provider_urlpatterns(),
]
