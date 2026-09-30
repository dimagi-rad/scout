"""providers_view maps each refresh outcome to a status the Connections page can act on (#779).

Only a dead credential reads "expired" (Reconnect). A refresh that merely couldn't run
reads "unavailable", because reconnecting can't fix a provider blip or Scout's own
invalid_client (#776).
"""

from datetime import timedelta
from unittest import mock

import httpx
import pytest
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib.sites.models import Site
from django.test import Client
from django.utils import timezone

from apps.users import auth_views
from apps.users.models import TenantConnection
from apps.users.services.token_refresh import (
    TokenRefreshDeadlineExceeded,
    TokenRefreshError,
    TokenRefreshRejected,
    TokenRefreshUnavailable,
    credential_fingerprint,
)

TOKEN_URL = "https://www.commcarehq.org/oauth/token/"


@pytest.fixture
def site(db):
    obj, _ = Site.objects.get_or_create(
        id=1, defaults={"domain": "testserver", "name": "Test Server"}
    )
    return obj


@pytest.fixture
def commcare_token(user, site):
    app = SocialApp.objects.create(
        provider="commcare", name="CommCare HQ", client_id="client", secret="secret"
    )
    app.sites.add(site)
    account = SocialAccount.objects.create(user=user, provider="commcare", uid="acct")
    token = SocialToken.objects.create(
        account=account,
        app=app,
        token="old-access",
        token_secret="old-refresh",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    return token


def _commcare_status(user) -> str:
    client = Client()
    client.force_login(user)
    providers = {p["id"]: p for p in client.get("/api/auth/providers/").json()["providers"]}
    return providers["commcare"]["status"]


def _refresh_raises(error):
    return mock.patch.object(auth_views, "refresh_oauth_token", mock.AsyncMock(side_effect=error))


@pytest.mark.django_db(transaction=True)
class TestProvidersViewRefreshOutcomes:
    @pytest.mark.parametrize(
        ("status_code", "body", "expected"),
        [
            (400, {"error": "invalid_grant"}, "expired"),
            (400, {"error": "invalid_client"}, "unavailable"),
            (401, {"error": "invalid_client"}, "unavailable"),
            (503, {}, "unavailable"),
            (429, {}, "unavailable"),
            (400, {"error": "invalid_request"}, "expired"),
        ],
    )
    def test_provider_answer_maps_to_status(
        self, user, commcare_token, httpx_mock, status_code, body, expected
    ):
        httpx_mock.add_response(method="POST", url=TOKEN_URL, status_code=status_code, json=body)

        assert _commcare_status(user) == expected

    def test_unreachable_provider_is_unavailable(self, user, commcare_token, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("down"), method="POST", url=TOKEN_URL)

        assert _commcare_status(user) == "unavailable"

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (TokenRefreshUnavailable("blip"), "unavailable"),
            (TokenRefreshDeadlineExceeded("contended"), "unavailable"),
            (TokenRefreshRejected("spent grant, could not store"), "expired"),
            (TokenRefreshError("non-transient"), "expired"),
        ],
    )
    def test_error_class_maps_to_status(self, user, commcare_token, error, expected):
        with _refresh_raises(error):
            assert _commcare_status(user) == expected

    def test_reconnect_marker_outranks_unavailable(self, user, commcare_token):
        TenantConnection.objects.update(
            oauth_refresh_failure_fingerprint=credential_fingerprint(commcare_token)
        )

        with _refresh_raises(TokenRefreshUnavailable("blip")):
            assert _commcare_status(user) == "expired"

    def test_successful_refresh_stays_connected(self, user, commcare_token, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url=TOKEN_URL,
            json={"access_token": "new", "refresh_token": "r2", "expires_in": 900},
        )

        assert _commcare_status(user) == "connected"


@pytest.fixture
def two_ocs_teams(user, site):
    app = SocialApp.objects.create(provider="ocs", name="OCS", client_id="c", secret="s")
    app.sites.add(site)
    for team in ("acme", "globex"):
        account = SocialAccount.objects.create(user=user, provider="ocs", uid=f"1#{team}")
        SocialToken.objects.create(
            account=account,
            app=app,
            token=team,
            token_secret="refresh",
            expires_at=timezone.now() - timedelta(minutes=1),
        )


def _ocs_status(user, outcomes: dict[str, Exception | None]) -> str:
    async def refresh(social_token, *args, **kwargs):
        error = outcomes[social_token.token]
        if error is not None:
            raise error

    client = Client()
    client.force_login(user)
    with mock.patch.object(auth_views, "refresh_oauth_token", refresh):
        providers = {p["id"]: p for p in client.get("/api/auth/providers/").json()["providers"]}
    return providers["ocs"]["status"]


@pytest.mark.django_db(transaction=True)
class TestProvidersViewAcrossIdentities:
    def test_dead_identity_outranks_unavailable_one(self, user, two_ocs_teams):
        outcomes = {"acme": TokenRefreshRejected("dead"), "globex": TokenRefreshUnavailable("blip")}

        assert _ocs_status(user, outcomes) == "expired"

    def test_unavailable_identity_outranks_nothing_known(self, user, two_ocs_teams):
        outcomes = {"acme": TokenRefreshUnavailable("blip"), "globex": TokenRefreshUnavailable("x")}

        assert _ocs_status(user, outcomes) == "unavailable"

    def test_working_identity_keeps_provider_connected(self, user, two_ocs_teams):
        outcomes = {"acme": TokenRefreshUnavailable("blip"), "globex": None}

        assert _ocs_status(user, outcomes) == "connected"
