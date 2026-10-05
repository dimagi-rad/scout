"""Connecting OCS teams by slug, singly or chained, from the ``teams`` claim (OCS #4685)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.test import Client
from django.utils import timezone

from apps.users.providers.ocs.provider import OCSProvider
from apps.users.services import ocs_team_flow

SUB = "ocs-user-1"
TEAMS = [
    {"slug": "alpha", "name": "Alpha"},
    {"slug": "beta", "name": "Beta"},
    {"slug": "gamma", "name": "Gamma"},
]
CONNECTIONS = "/settings/connections"


@pytest.fixture(autouse=True)
def _no_tenant_resolution(mocker):
    mocker.patch("apps.users.signals.resolve_ocs_chatbots", new=AsyncMock())


@pytest.fixture
def client(user, ocs_app):
    client = Client()
    client.force_login(user)
    return client


def _identity(user, app, team, *, teams=TEAMS, token=True, last_login=None):
    extra = {"sub": SUB, "team": team}
    if teams is not None:
        extra["teams"] = teams
    account = SocialAccount.objects.create(
        user=user, provider="ocs", uid=f"{SUB}#{team}", extra_data=extra
    )
    if last_login:
        SocialAccount.objects.filter(pk=account.pk).update(last_login=last_login)
        account.refresh_from_db()
    if token:
        SocialToken.objects.create(
            account=account,
            app=app,
            token=f"tok-{team}",
            token_secret="refresh",
            expires_at=timezone.now() + timedelta(hours=1),
        )
    return account


def _start(client, team=None, **extra):
    data = {"process": "connect", "next": CONNECTIONS, **extra}
    if team is not None:
        data["team"] = team
    response = client.post("/accounts/ocs/login/", data)
    assert response.status_code == 302
    location = urlparse(response["Location"])
    return response, parse_qs(location.query)


def _callback(client, mocker, query, userinfo):
    mocker.patch(
        "apps.users.providers.ocs.views.OCSOAuth2Adapter.get_access_token_data",
        return_value={"access_token": "new-token", "refresh_token": "r", "expires_in": 3600},
    )
    response_obj = MagicMock(status_code=200)
    response_obj.json.return_value = userinfo
    mocker.patch("apps.users.providers.ocs.views.requests.get", return_value=response_obj)
    return client.get(
        "/accounts/ocs/login/callback/", {"state": query["state"][0], "code": "the-code"}
    )


def _userinfo(team, teams=TEAMS):
    info = {"sub": SUB, "team": team}
    if teams is not None:
        info["teams"] = teams
    return info


def _flow(client):
    return client.session.get(ocs_team_flow.SESSION_KEY)


class TestClaims:
    def test_teams_scope_is_requested(self):
        assert "teams" in OCSProvider.get_default_scope(None)

    def test_teams_scope_can_be_turned_off_for_an_older_ocs(self, settings):
        settings.OCS_REQUEST_TEAMS_SCOPE = False
        assert "teams" not in OCSProvider.get_default_scope(None)

    def test_teams_claim_is_sanitized_and_sorted(self):
        claims = {
            "teams": [
                {"slug": "zeta", "name": "Zeta"},
                {"slug": "bad slug", "name": "x"},
                "nope",
                {"slug": "alpha"},
                {"slug": "zeta", "name": "dup"},
            ]
        }
        assert ocs_team_flow.teams_from_claims(claims) == [
            {"slug": "alpha", "name": "alpha"},
            {"slug": "zeta", "name": "Zeta"},
        ]

    def test_a_missing_claim_is_unknown_not_empty(self):
        assert ocs_team_flow.teams_from_claims({"team": "a"}) is None
        assert ocs_team_flow.teams_from_claims({"teams": []}) == []

    @pytest.mark.django_db
    def test_known_teams_prefers_the_freshest_identity_per_subject(self, user, ocs_app):
        now = timezone.now()
        _identity(user, ocs_app, "alpha", teams=TEAMS, last_login=now - timedelta(days=2))
        _identity(user, ocs_app, "beta", teams=TEAMS[:2], last_login=now)
        _identity(user, ocs_app, "legacy", teams=None, last_login=now + timedelta(days=1))

        teams = ocs_team_flow.known_teams(SocialAccount.objects.filter(user=user))

        assert [t["slug"] for t in teams] == ["alpha", "beta"]

    @pytest.mark.django_db
    def test_no_identity_with_the_claim_means_unknown(self, user, ocs_app):
        _identity(user, ocs_app, "alpha", teams=None)
        assert ocs_team_flow.known_teams(SocialAccount.objects.filter(user=user)) is None


@pytest.mark.django_db
class TestPinnedStart:
    def test_pins_the_team_and_skips_repeat_consent(self, client):
        _response, query = _start(client, "beta")

        assert query["team"] == ["beta"]
        assert query["approval_prompt"] == ["auto"]
        assert "teams" in query["scope"][0].split()
        flow = _flow(client)
        assert flow["mode"] == ocs_team_flow.MODE_ONE
        assert flow["pending"] == "beta"

    def test_an_unpinned_connect_is_unchanged(self, client):
        _response, query = _start(client)

        assert "team" not in query
        assert "approval_prompt" not in query
        assert _flow(client) is None

    def test_a_malformed_slug_is_not_forwarded(self, client):
        _response, query = _start(client, "../evil?x=1")

        assert "team" not in query
        assert _flow(client) is None

    def test_generic_auth_params_cannot_pin_a_team(self, client):
        response = client.post(
            "/accounts/ocs/login/?auth_params=team%3Dbeta",
            {"process": "connect", "next": CONNECTIONS},
        )

        assert "team" not in parse_qs(urlparse(response["Location"]).query)

    def test_a_sign_in_is_never_pinned(self, ocs_app):
        response = Client().post("/accounts/ocs/login/", {"process": "login", "team": "beta"})

        assert "team" not in parse_qs(urlparse(response["Location"]).query)

    def test_a_get_does_not_start_or_record_a_flow(self, client):
        response = client.get("/accounts/ocs/login/", {"process": "connect", "team": "beta"})

        assert response.status_code == 200
        assert _flow(client) is None


@pytest.mark.django_db
class TestCallback:
    def test_matching_team_connects_and_stores_the_team_list(self, client, user, ocs_app, mocker):
        _identity(user, ocs_app, "alpha", teams=None)
        _response, query = _start(client, "beta")

        response = _callback(client, mocker, query, _userinfo("beta"))

        assert response.status_code == 302
        account = SocialAccount.objects.get(uid=f"{SUB}#beta")
        assert account.user == user
        assert account.extra_data["teams"] == TEAMS
        body = client.get("/api/auth/ocs/teams/").json()
        assert body["flow"] is None
        assert {t["slug"]: t["connected"] for t in body["teams"]} == {
            "alpha": True,
            "beta": True,
            "gamma": False,
        }

    def test_a_fallback_team_is_refused_and_reported(self, client, user, ocs_app, mocker):
        alpha = _identity(user, ocs_app, "alpha", teams=TEAMS)
        _response, query = _start(client, "gamma")
        fresh = TEAMS[:2]

        response = _callback(client, mocker, query, _userinfo("beta", teams=fresh))

        assert response.status_code == 302
        assert response["Location"] == CONNECTIONS
        assert not SocialAccount.objects.filter(uid=f"{SUB}#beta").exists()
        assert _flow(client)["stopped"] == {
            "reason": ocs_team_flow.STOP_MISMATCH,
            "team": "gamma",
            "got": "beta",
        }
        alpha.refresh_from_db()
        assert alpha.extra_data["teams"] == fresh
        body = client.get("/api/auth/ocs/teams/").json()
        assert body["flow"]["stopped"]["got"] == {"slug": "beta", "name": "Beta"}
        assert body["next"] is None

    def test_a_fallback_to_a_removed_team_does_not_revive_its_token(
        self, client, user, ocs_app, mocker
    ):
        _identity(user, ocs_app, "alpha")
        removed = _identity(user, ocs_app, "beta", token=False)
        _response, query = _start(client, "gamma")

        _callback(client, mocker, query, _userinfo("beta"))

        assert not SocialToken.objects.filter(account=removed).exists()
        assert _flow(client)["stopped"]["reason"] == ocs_team_flow.STOP_MISMATCH

    def test_a_missing_team_claim_is_a_mismatch(self, client, user, ocs_app, mocker):
        _response, query = _start(client, "beta")

        _callback(client, mocker, query, {"sub": SUB})

        assert not SocialAccount.objects.filter(user=user).exists()
        assert _flow(client)["stopped"]["reason"] == ocs_team_flow.STOP_MISMATCH

    def test_an_unpinned_connect_is_not_checked(self, client, user, ocs_app, mocker):
        _response, query = _start(client)

        _callback(client, mocker, query, _userinfo("beta"))

        assert SocialAccount.objects.filter(uid=f"{SUB}#beta", user=user).exists()

    def test_cancelling_on_ocs_returns_to_connections(self, client):
        _response, query = _start(client, "beta")

        response = client.get(
            "/accounts/ocs/login/callback/",
            {"state": query["state"][0], "error": "access_denied"},
        )

        assert response.status_code == 302
        assert response["Location"] == CONNECTIONS
        assert _flow(client)["stopped"] == {
            "reason": ocs_team_flow.STOP_CANCELLED,
            "team": "beta",
        }

    def test_an_unpinned_cancel_keeps_allauth_handling(self, client):
        _response, query = _start(client)

        response = client.get(
            "/accounts/ocs/login/callback/",
            {"state": query["state"][0], "error": "access_denied"},
        )

        assert response["Location"] != CONNECTIONS
        assert _flow(client) is None

    def test_the_requested_team_lives_in_oauth_state(self, client):
        _start(client, "beta")

        states = client.session["socialaccount_states"]
        assert [s[0][ocs_team_flow.REQUESTED_TEAM_STATE_KEY] for s in states.values()] == ["beta"]


@pytest.mark.django_db
class TestChain:
    def test_connects_each_remaining_team_in_turn(self, client, user, ocs_app, mocker):
        _identity(user, ocs_app, "alpha")

        started = client.post("/api/auth/ocs/teams/connect-all/").json()
        assert started["next"] == "beta"
        assert [t["slug"] for t in started["flow"]["remaining"]] == ["beta", "gamma"]

        _response, query = _start(client, "beta")
        _callback(client, mocker, query, _userinfo("beta"))
        body = client.get("/api/auth/ocs/teams/").json()
        assert body["next"] == "gamma"
        assert body["flow"]["connected"] == [{"slug": "beta", "name": "Beta"}]

        _response, query = _start(client, "gamma")
        _callback(client, mocker, query, _userinfo("gamma"))
        body = client.get("/api/auth/ocs/teams/").json()
        assert body["next"] is None
        assert body["flow"]["finished"] is True
        assert [t["slug"] for t in body["flow"]["connected"]] == ["beta", "gamma"]
        assert client.get("/api/auth/ocs/teams/").json()["flow"] is None

    def test_a_hop_still_on_ocs_is_not_declared_incomplete(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")
        _start(client, "beta")

        body = client.get("/api/auth/ocs/teams/").json()

        assert body["next"] is None
        assert body["flow"]["stopped"] is None
        assert body["flow"]["pending"] == {"slug": "beta", "name": "Beta"}

    def test_an_incomplete_hop_that_lands_late_pauses_before_the_next(
        self, client, user, ocs_app, mocker
    ):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")
        _response, query = _start(client, "beta")
        session = client.session
        session[ocs_team_flow.SESSION_KEY]["updated_at"] -= ocs_team_flow.PENDING_GRACE_SECONDS + 1
        session.save()
        assert client.get("/api/auth/ocs/teams/").json()["flow"]["stopped"]["reason"] == (
            ocs_team_flow.STOP_INCOMPLETE
        )

        _callback(client, mocker, query, _userinfo("beta"))
        body = client.get("/api/auth/ocs/teams/").json()

        assert body["flow"]["connected"] == [{"slug": "beta", "name": "Beta"}]
        assert body["flow"]["stopped"]["reason"] == ocs_team_flow.STOP_IDLE
        assert body["flow"]["stopped"]["team"]["slug"] == "gamma"
        assert body["next"] is None

    def test_a_hop_that_never_returns_stops_the_chain(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")
        _start(client, "beta")
        session = client.session
        session[ocs_team_flow.SESSION_KEY]["updated_at"] -= ocs_team_flow.PENDING_GRACE_SECONDS + 1
        session.save()

        body = client.get("/api/auth/ocs/teams/").json()

        assert body["next"] is None
        assert body["flow"]["stopped"]["reason"] == ocs_team_flow.STOP_INCOMPLETE
        assert body["flow"]["stopped"]["team"]["slug"] == "beta"

    def test_a_mismatch_stops_the_chain_and_resume_restarts_it(self, client, user, ocs_app, mocker):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")
        _response, query = _start(client, "beta")
        _callback(client, mocker, query, _userinfo("alpha"))

        body = client.get("/api/auth/ocs/teams/").json()
        assert body["next"] is None
        assert body["flow"]["stopped"]["reason"] == ocs_team_flow.STOP_MISMATCH

        resumed = client.post("/api/auth/ocs/teams/connect-all/").json()
        assert resumed["next"] == "beta"

    def test_a_pinned_connect_off_the_queue_replaces_the_chain(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")

        _start(client, "gamma")

        assert _flow(client)["mode"] == ocs_team_flow.MODE_ONE

    def test_restarting_the_head_from_a_second_tab_keeps_the_chain(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")
        _start(client, "beta")

        _start(client, "beta")

        assert _flow(client)["mode"] == ocs_team_flow.MODE_ALL
        assert _flow(client)["queue"] == ["beta", "gamma"]

    def test_an_idle_chain_does_not_resume_by_itself(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")
        session = client.session
        session[ocs_team_flow.SESSION_KEY]["updated_at"] -= ocs_team_flow.CHAIN_IDLE_SECONDS + 1
        session.save()

        body = client.get("/api/auth/ocs/teams/").json()

        assert body["next"] is None
        assert body["flow"]["stopped"]["reason"] == ocs_team_flow.STOP_IDLE

    def test_stop_keeps_progress_and_halts(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")

        body = client.post("/api/auth/ocs/teams/stop/").json()

        assert body["next"] is None
        assert body["flow"]["stopped"]["reason"] == ocs_team_flow.STOP_USER
        assert body["flow"]["stopped"]["team"]["slug"] == "beta"

    def test_stop_halts_a_hop_still_in_its_grace_period(self, client, user, ocs_app, mocker):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")
        _response, query = _start(client, "beta")

        stopped = client.post("/api/auth/ocs/teams/stop/").json()
        _callback(client, mocker, query, _userinfo("beta"))
        body = client.get("/api/auth/ocs/teams/").json()

        assert stopped["flow"]["stopped"]["team"]["slug"] == "beta"
        assert body["next"] is None
        assert body["flow"]["connected"] == [{"slug": "beta", "name": "Beta"}]
        assert body["flow"]["stopped"]["reason"] == ocs_team_flow.STOP_USER
        assert body["flow"]["stopped"]["team"]["slug"] == "gamma"
        assert [t["slug"] for t in body["flow"]["remaining"]] == ["gamma"]

    def test_a_hop_the_browser_could_not_start_is_reported_as_failed(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")

        body = client.post(
            "/api/auth/ocs/teams/stop/", {"reason": "failed"}, content_type="application/json"
        ).json()

        assert body["flow"]["stopped"]["reason"] == ocs_team_flow.STOP_FAILED

    def test_dismiss_forgets_the_flow(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        client.post("/api/auth/ocs/teams/connect-all/")

        assert client.post("/api/auth/ocs/teams/dismiss/").status_code == 200

        assert _flow(client) is None

    def test_connect_all_needs_a_known_list(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha", teams=None)

        response = client.post("/api/auth/ocs/teams/connect-all/")

        assert response.status_code == 400
        assert "Reconnect" in response.json()["error"]

    def test_nothing_to_connect_is_rejected(self, client, user, ocs_app):
        for team in ("alpha", "beta", "gamma"):
            _identity(user, ocs_app, team)

        assert client.post("/api/auth/ocs/teams/connect-all/").status_code == 400

    def test_a_removed_connection_counts_as_unconnected(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha")
        _identity(user, ocs_app, "beta", token=False)

        body = client.get("/api/auth/ocs/teams/").json()

        assert {t["slug"] for t in body["teams"] if not t["connected"]} == {"beta", "gamma"}


@pytest.mark.django_db
class TestEndpoints:
    def test_legacy_connections_report_an_unknown_list(self, client, user, ocs_app):
        _identity(user, ocs_app, "alpha", teams=None)

        body = client.get("/api/auth/ocs/teams/").json()

        assert body == {
            "available": True,
            "known": False,
            "teams": [],
            "flow": None,
            "next": None,
        }

    def test_reports_when_the_teams_scope_is_off(self, client, user, ocs_app, settings):
        settings.OCS_REQUEST_TEAMS_SCOPE = False
        _identity(user, ocs_app, "alpha")

        body = client.get("/api/auth/ocs/teams/").json()

        assert body["available"] is False
        assert body["known"] is True

    @pytest.mark.parametrize("action", ["connect-all", "stop", "dismiss"])
    def test_chain_actions_require_csrf(self, user, ocs_app, action):
        _identity(user, ocs_app, "alpha")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)

        assert client.post(f"/api/auth/ocs/teams/{action}/").status_code == 403

    def test_requires_authentication(self, ocs_app):
        assert Client().get("/api/auth/ocs/teams/").status_code == 401
        assert Client().post("/api/auth/ocs/teams/connect-all/").status_code == 401

    def test_the_pinned_login_requires_csrf(self, user, ocs_app):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)

        response = client.post("/accounts/ocs/login/", {"process": "connect", "team": "beta"})

        assert response.status_code == 403
        assert client.session.get(ocs_team_flow.SESSION_KEY) is None
