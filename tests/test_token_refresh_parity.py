"""Both renewal entry points must reach the same verdict for the same provider outcome.

``refresh_oauth_token_result`` (async, proactive) and ``refresh_oauth_token_result_sync``
(blocking, mid-run) carry parallel decision logic. A fix that reaches only one of them has
shipped twice already (#373, then the ``record_failure`` drift), so this matrix drives both
through identical cases and compares what a caller can observe: the raised class or result,
the reconnect marker on the connection, and the severity of what was logged.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest
import requests
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from asgiref.sync import sync_to_async
from django.db import DatabaseError
from django.utils import timezone

from apps.users.models import TenantConnection
from apps.users.services import token_refresh
from apps.users.services.token_refresh import (
    TokenRefreshDeadlineExceeded,
    TokenRefreshError,
    TokenRefreshRejected,
    TokenRefreshUnavailable,
    classify_http_failure,
    classify_persist_failure,
    refresh_oauth_token_result,
    refresh_oauth_token_result_sync,
)

URL = "https://provider.example/o/token/"
ROTATED = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 900}
UNROTATED = {"access_token": "new-access", "expires_in": 900}
NOTHING_LOGGED = 0


@pytest.fixture
def oauth_identity(user):
    app = SocialApp.objects.create(
        provider="commcare", name="CommCare", client_id="client", secret="app-secret"
    )
    account = SocialAccount.objects.create(user=user, provider="commcare", uid="account")
    token = SocialToken.objects.create(
        account=account,
        app=app,
        token="old-access",
        token_secret="old-refresh",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    connection = TenantConnection.objects.create(
        user=user,
        provider="commcare",
        credential_type=TenantConnection.OAUTH,
        social_account=account,
    )
    return token, connection


def _status(code, body=None):
    return ("status", code, body if body is not None else {})


def _raises(httpx_exc, requests_exc):
    return ("raises", httpx_exc, requests_exc)


def _register_provider(mode, spec, httpx_mock, requests_mock):
    if spec[0] == "status":
        _, code, body = spec
        if mode == "async":
            httpx_mock.add_response(url=URL, status_code=code, json=body)
        else:
            requests_mock.post(URL, status_code=code, json=body)
    else:
        _, httpx_exc, requests_exc = spec
        if mode == "async":
            httpx_mock.add_exception(httpx_exc, url=URL)
        else:
            requests_mock.post(URL, exc=requests_exc)


def _install_persist_hook(mode, hook, token, monkeypatch):
    """Make the persist step fail, or lose the CAS race, the same way in either transport."""
    if hook is None:
        return
    error = {
        "database_error": DatabaseError("persist failed"),
        "deadline": token_refresh._RefreshDeadlineExceeded(),
    }.get(hook)
    changes = {
        "advance": {"token": "winner-access", "token_secret": "winner-refresh"},
        "touch_expiry": {"expires_at": timezone.now() + timedelta(hours=1)},
    }.get(hook)

    if mode == "async":
        original = token_refresh._apersist_refresh_response

        async def persist(*args, **kwargs):
            if error:
                raise error
            await SocialToken.objects.filter(pk=token.pk).aupdate(**changes)
            return await original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_apersist_refresh_response", persist)
    else:
        original = token_refresh._persist_refresh_response

        def persist(*args, **kwargs):
            if error:
                raise error
            SocialToken.objects.filter(pk=token.pk).update(**changes)
            return original(*args, **kwargs)

        monkeypatch.setattr(token_refresh, "_persist_refresh_response", persist)


def _describe(outcome) -> str:
    if isinstance(outcome, BaseException):
        return type(outcome).__name__
    return f"{outcome.status.value}:advanced={outcome.credential_advanced}"


async def _run(mode, token):
    try:
        if mode == "async":
            return await refresh_oauth_token_result(token, URL)
        return await sync_to_async(refresh_oauth_token_result_sync)(token, URL)
    except TokenRefreshError as exc:
        return exc


async def _marker_recorded(connection) -> bool:
    fresh = await TenantConnection.objects.aget(pk=connection.pk)
    return fresh.oauth_refresh_failure_fingerprint != ""


# id, provider response, persist hook, outcome, marker recorded, highest severity logged
CASES = [
    ("applied", _status(200, ROTATED), None, "applied:advanced=True", False, logging.INFO),
    (
        "invalid-grant",
        _status(400, {"error": "invalid_grant"}),
        None,
        "TokenRefreshRejected",
        True,
        logging.WARNING,
    ),
    (
        "invalid-client",
        _status(400, {"error": "invalid_client"}),
        None,
        "TokenRefreshError",
        True,
        logging.WARNING,
    ),
    ("other-4xx", _status(404), None, "TokenRefreshError", True, logging.WARNING),
    ("throttled-429", _status(429), None, "TokenRefreshUnavailable", False, logging.WARNING),
    ("server-500", _status(500), None, "TokenRefreshUnavailable", False, logging.ERROR),
    (
        "connection-error",
        _raises(httpx.ConnectError("down"), requests.ConnectionError("down")),
        None,
        "TokenRefreshUnavailable",
        False,
        logging.ERROR,
    ),
    (
        "unexpected-error",
        _raises(RuntimeError("bug"), RuntimeError("bug")),
        None,
        "TokenRefreshError",
        True,
        logging.ERROR,
    ),
    (
        "malformed-200",
        _status(200, {"error": "nope"}),
        None,
        "TokenRefreshError",
        False,
        NOTHING_LOGGED,
    ),
    (
        "persist-database-error",
        _status(200, UNROTATED),
        "database_error",
        "TokenRefreshUnavailable",
        False,
        logging.WARNING,
    ),
    (
        "persist-database-error-spent-grant",
        _status(200, ROTATED),
        "database_error",
        "TokenRefreshUnavailable",
        False,
        logging.WARNING,
    ),
    (
        "persist-deadline-grant-kept",
        _status(200, UNROTATED),
        "deadline",
        "TokenRefreshDeadlineExceeded",
        False,
        logging.WARNING,
    ),
    (
        "persist-deadline-grant-spent",
        _status(200, ROTATED),
        "deadline",
        "TokenRefreshRejected",
        False,
        logging.WARNING,
    ),
    (
        "superseded-credential-advanced",
        _status(200, ROTATED),
        "advance",
        "superseded:advanced=True",
        False,
        logging.WARNING,
    ),
    (
        "superseded-credential-not-advanced",
        _status(200, ROTATED),
        "touch_expiry",
        "superseded:advanced=False",
        True,
        logging.WARNING,
    ),
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    ("provider", "hook", "outcome", "marker", "severity"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_both_transports_reach_the_same_verdict(
    oauth_identity,
    mode,
    provider,
    hook,
    outcome,
    marker,
    severity,
    httpx_mock,
    requests_mock,
    monkeypatch,
    caplog,
):
    token, connection = oauth_identity
    _register_provider(mode, provider, httpx_mock, requests_mock)
    _install_persist_hook(mode, hook, token, monkeypatch)
    caplog.set_level(logging.DEBUG, logger=token_refresh.logger.name)

    result = await _run(mode, token)

    assert _describe(result) == outcome
    assert await _marker_recorded(connection) is marker
    logged = max(
        (r.levelno for r in caplog.records if r.name == token_refresh.logger.name),
        default=NOTHING_LOGGED,
    )
    assert logged == severity


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_both_transports_refuse_a_token_without_an_application(oauth_identity, mode):
    token, connection = oauth_identity
    token.app = None

    result = await _run(mode, token)

    assert _describe(result) == "TokenRefreshError"
    assert await _marker_recorded(connection) is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("deadline", [False, True], ids=["database-error", "deadline"])
async def test_both_transports_classify_a_failed_preflight_read(
    oauth_identity, mode, deadline, monkeypatch
):
    token, connection = oauth_identity
    error = token_refresh._RefreshDeadlineExceeded() if deadline else DatabaseError("down")

    def preflight(*args, **kwargs):
        raise error

    monkeypatch.setattr(token_refresh, "_preflight_token", preflight)

    result = await _run(mode, token)

    expected = "TokenRefreshDeadlineExceeded" if deadline else "TokenRefreshUnavailable"
    assert _describe(result) == expected
    assert await _marker_recorded(connection) is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome"),
    [(400, "TokenRefreshRejected"), (404, "TokenRefreshError"), (500, "TokenRefreshUnavailable")],
)
async def test_async_record_failure_false_keeps_the_verdict_but_leaves_no_marker(
    oauth_identity, status, outcome, httpx_mock
):
    token, connection = oauth_identity
    httpx_mock.add_response(url=URL, status_code=status, json={"error": "invalid_grant"})

    with pytest.raises(TokenRefreshError) as excinfo:
        await refresh_oauth_token_result(token, URL, record_failure=False)

    assert type(excinfo.value).__name__ == outcome
    assert await _marker_recorded(connection) is False


@pytest.mark.parametrize(
    ("status", "flags", "error", "marker", "level", "reason"),
    [
        (400, {"rejected": True}, TokenRefreshRejected, True, logging.WARNING, "invalid_grant"),
        (401, {"misconfigured": True}, TokenRefreshError, True, logging.WARNING, "invalid_client"),
        (404, {}, TokenRefreshError, True, logging.WARNING, "other"),
        (429, {"transient": True}, TokenRefreshUnavailable, False, logging.WARNING, "other"),
        (503, {"transient": True}, TokenRefreshUnavailable, False, logging.ERROR, "other"),
        (None, {}, TokenRefreshError, True, logging.ERROR, "other"),
        (None, {"transient": True}, TokenRefreshUnavailable, False, logging.ERROR, "other"),
    ],
)
def test_classify_http_failure(status, flags, error, marker, level, reason):
    verdict = classify_http_failure(
        status,
        transient=flags.get("transient", False),
        rejected=flags.get("rejected", False),
        misconfigured=flags.get("misconfigured", False),
        cause=RuntimeError("boom"),
    )

    assert type(verdict.error) is error
    assert (verdict.record_marker, verdict.log_level, verdict.reason) == (marker, level, reason)


@pytest.mark.parametrize(
    ("exc", "grant_spent", "error"),
    [
        (token_refresh._RefreshDeadlineExceeded(), True, TokenRefreshRejected),
        (token_refresh._RefreshDeadlineExceeded(), False, TokenRefreshDeadlineExceeded),
        (DatabaseError("down"), True, TokenRefreshUnavailable),
        (DatabaseError("down"), False, TokenRefreshUnavailable),
    ],
)
def test_classify_persist_failure(exc, grant_spent, error):
    classified, _message = classify_persist_failure(exc, grant_spent=grant_spent)

    assert type(classified) is error
