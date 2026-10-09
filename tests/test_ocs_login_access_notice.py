"""An OCS 403 at sign-in is the user's team permission, not a Scout fault (SCOUT-DJANGO-3E).

The user is told through a notice on ``/api/auth/me/`` instead of Sentry being paged.
Any other OCS failure still pages.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apps.common.errors import OCSAuthError
from apps.users import signals
from apps.users.services import ocs_access_notice

SIGNALS_LOGGER = "apps.users.signals"
DENIED = {"team": {"slug": "acme", "name": "Acme Health"}}


def _sociallogin():
    return SimpleNamespace(
        account=SimpleNamespace(
            provider="ocs",
            uid="sub-1#acme",
            extra_data={"teams": [{"slug": "acme", "name": "Acme Health"}]},
        ),
        token=SimpleNamespace(token="tok"),
        user=SimpleNamespace(pk=1, email="u@b.c"),
    )


def _login(resolver, session):
    request = SimpleNamespace(session=session)
    with (
        patch.object(signals, "resolve_ocs_chatbots", resolver),
        patch.object(signals, "resolve_pending_invites_on_login"),
    ):
        signals.resolve_tenant_on_social_login(request=request, sociallogin=_sociallogin())


def test_403_warns_and_leaves_a_notice(caplog):
    session = {}
    with caplog.at_level(logging.WARNING, logger=SIGNALS_LOGGER):
        _login(AsyncMock(side_effect=OCSAuthError("refused", status_code=403)), session)

    levels = {r.levelno for r in caplog.records if r.name == SIGNALS_LOGGER}
    assert levels == {logging.WARNING}
    assert session[ocs_access_notice.SESSION_KEY] == DENIED


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


def test_a_successful_ocs_login_clears_the_notice():
    session = {ocs_access_notice.SESSION_KEY: DENIED}
    _login(AsyncMock(return_value=[]), session)
    assert ocs_access_notice.SESSION_KEY not in session


def test_teamless_identity_names_no_team():
    account = SimpleNamespace(provider="ocs", uid="sub-1", extra_data={})
    assert ocs_access_notice.notice_for(account) == {"team": None}


@pytest.mark.django_db
class TestMeReportsTheNotice:
    def test_absent_by_default(self, client, user):
        client.force_login(user)
        assert client.get("/api/auth/me/").json()["ocs_access_denied"] is None

    def test_reported_until_dismissed(self, client, user):
        client.force_login(user)
        session = client.session
        session[ocs_access_notice.SESSION_KEY] = DENIED
        session.save()

        assert client.get("/api/auth/me/").json()["ocs_access_denied"] == DENIED
        assert client.post("/api/auth/ocs/access-notice/dismiss/").status_code == 200
        assert client.get("/api/auth/me/").json()["ocs_access_denied"] is None

    def test_dismiss_requires_login(self, client):
        assert client.post("/api/auth/ocs/access-notice/dismiss/").status_code == 401
