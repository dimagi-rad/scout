"""An OCS 403 at sign-in is the user's team permission, not a Scout fault (SCOUT-DJANGO-3E).

The user is told through a notice on ``/api/auth/me/`` instead of Sentry being paged.
Any other OCS failure still pages.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import login
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import RequestFactory

from apps.common.errors import OCSAuthError
from apps.users import auth_views, signals
from apps.users.services import ocs_access_notice
from apps.users.services.onboarding_cache import me_onboarding_cache_key

SIGNALS_LOGGER = "apps.users.signals"
ACME = {"slug": "acme", "name": "Acme Health"}
BETA = {"slug": "beta", "name": "Beta"}


def _account(slug):
    return SimpleNamespace(
        provider="ocs",
        uid=f"sub-1#{slug}",
        extra_data={"teams": [ACME, BETA]},
    )


def _login(resolver, session, slug="acme"):
    request = SimpleNamespace(session=session)
    sociallogin = SimpleNamespace(
        account=_account(slug),
        token=SimpleNamespace(token="tok"),
        user=SimpleNamespace(pk=1, email="u@b.c"),
    )
    with (
        patch.object(signals, "resolve_ocs_chatbots", resolver),
        patch.object(signals, "resolve_pending_invites_on_login"),
    ):
        signals.resolve_tenant_on_social_login(request=request, sociallogin=sociallogin)


def _refused():
    return AsyncMock(side_effect=OCSAuthError("refused", status_code=403))


def test_403_warns_and_leaves_a_notice(caplog):
    session = {}
    with caplog.at_level(logging.WARNING, logger=SIGNALS_LOGGER):
        _login(_refused(), session)

    levels = {r.levelno for r in caplog.records if r.name == SIGNALS_LOGGER}
    assert levels == {logging.WARNING}
    assert ocs_access_notice.payload(session[ocs_access_notice.SESSION_KEY]) == {"teams": [ACME]}


@pytest.mark.parametrize(
    "error",
    [OCSAuthError("expired", status_code=401), RuntimeError("provider 503")],
)
def test_other_failures_still_page_without_a_notice(error, caplog):
    session = {}
    with caplog.at_level(logging.WARNING, logger=SIGNALS_LOGGER):
        _login(AsyncMock(side_effect=error), session)

    assert logging.ERROR in {r.levelno for r in caplog.records if r.name == SIGNALS_LOGGER}
    assert ocs_access_notice.SESSION_KEY not in session


def test_another_teams_success_keeps_the_refusal():
    """In a connect-all chain team A may be refused and team B then succeed."""
    session = {}
    _login(_refused(), session, slug="acme")
    _login(AsyncMock(return_value=[]), session, slug="beta")
    assert ocs_access_notice.payload(session[ocs_access_notice.SESSION_KEY]) == {"teams": [ACME]}


def test_the_refused_teams_own_success_clears_it():
    session = {}
    _login(_refused(), session, slug="acme")
    _login(_refused(), session, slug="beta")
    _login(AsyncMock(return_value=[]), session, slug="acme")
    assert ocs_access_notice.payload(session[ocs_access_notice.SESSION_KEY]) == {"teams": [BETA]}
    _login(AsyncMock(return_value=[]), session, slug="beta")
    assert ocs_access_notice.SESSION_KEY not in session


def test_teamless_identity_has_an_empty_slug():
    refused = ocs_access_notice.with_refusal(
        None, SimpleNamespace(provider="ocs", uid="sub-1", extra_data={})
    )
    assert ocs_access_notice.payload(refused) == {"teams": [{"slug": "", "name": ""}]}


@pytest.mark.django_db
def test_notice_survives_session_rotation_at_login(user):
    """The signal runs before allauth logs the user in; login() must keep the key."""
    request = RequestFactory().get("/accounts/ocs/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session[ocs_access_notice.SESSION_KEY] = {"acme": ACME}
    request.session.save()
    before = request.session.session_key

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    assert request.session.session_key != before
    assert request.session[ocs_access_notice.SESSION_KEY] == {"acme": ACME}


@pytest.mark.django_db
class TestMeReportsTheNotice:
    def _refuse(self, client, *teams):
        session = client.session
        session[ocs_access_notice.SESSION_KEY] = {t["slug"]: t for t in teams}
        session.save()

    def test_absent_by_default(self, client, user):
        client.force_login(user)
        assert client.get("/api/auth/me/").json()["ocs_access_denied"] is None

    def test_reported_until_dismissed(self, client, user):
        client.force_login(user)
        self._refuse(client, ACME)

        assert client.get("/api/auth/me/").json()["ocs_access_denied"] == {"teams": [ACME]}
        assert client.post("/api/auth/ocs/access-notice/dismiss/").status_code == 200
        assert client.get("/api/auth/me/").json()["ocs_access_denied"] is None

    def test_onboarding_resolve_clears_a_team_granted_since(self, client, user):
        client.force_login(user)
        self._refuse(client, ACME, BETA)
        cache.delete(me_onboarding_cache_key(user))

        async def tokens(_user, provider):
            return [(_account("acme"), "tok")] if provider == "ocs" else []

        with (
            patch.object(auth_views, "aiter_fresh_access_tokens", side_effect=tokens),
            patch.object(auth_views, "resolve_ocs_chatbots", AsyncMock(return_value=[object()])),
        ):
            denied = client.get("/api/auth/me/").json()["ocs_access_denied"]

        assert denied == {"teams": [BETA]}
        assert client.session[ocs_access_notice.SESSION_KEY] == {"beta": BETA}

    def test_dismiss_requires_login(self, client):
        assert client.post("/api/auth/ocs/access-notice/dismiss/").status_code == 401
