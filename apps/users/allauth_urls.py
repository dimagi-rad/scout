"""Narrowed allauth URL surface (arch #258, finding 13#9).

Scout's SPA owns the human-facing auth UI: email/password login lives at
``/api/auth/`` (rate-limited, CSRF-protected; there is no self-registration) and social login is initiated from
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

Provider routes are mounted for every installed provider, configured or not, so a
provider with no ``SocialApp`` (``commcare_eu`` until its credentials are set) 404s
instead of letting allauth's ``SocialApp.DoesNotExist`` surface as a 500.
"""

from functools import wraps

from allauth.socialaccount.models import SocialApp
from allauth.socialaccount.views import login_cancelled, login_error
from allauth.urls import build_provider_urlpatterns
from django.http import Http404
from django.urls import URLPattern, URLResolver, path
from django.views.generic.base import RedirectView


def _absent_when_unconfigured(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except SocialApp.DoesNotExist as exc:
            raise Http404("This sign-in provider is not configured.") from exc

    return wrapped


def _provider_urlpatterns(patterns):
    wrapped = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            wrapped.append(
                URLResolver(
                    pattern.pattern,
                    _provider_urlpatterns(pattern.url_patterns),
                    pattern.default_kwargs,
                    pattern.app_name,
                    pattern.namespace,
                )
            )
        else:
            wrapped.append(
                URLPattern(
                    pattern.pattern,
                    _absent_when_unconfigured(pattern.callback),
                    pattern.default_args,
                    pattern.name,
                )
            )
    return wrapped


# Note: allauth's LOGIN_REDIRECT_URL/LOGIN_URL and our adapter both reference the
# "account_login" view name. We keep the *name* resolvable but point it at the
# SPA root rather than the stock HTML login form. The SPA renders its own login
# UI and surfaces any queued allauth messages on the next page load.
urlpatterns = [
    path("login/", RedirectView.as_view(url="/", query_string=True), name="account_login"),
    path("login/cancelled/", login_cancelled, name="socialaccount_login_cancelled"),
    path("login/error/", login_error, name="socialaccount_login_error"),
    *_provider_urlpatterns(build_provider_urlpatterns()),
]
