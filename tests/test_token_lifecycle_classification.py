"""Token-lifecycle error classification and the unknown-expiry hole (#373).

Two independent defects in the same module:

1. ``refresh_oauth_token_sync`` logged an expected ``400 invalid_grant`` (a dead
   refresh token) via ``logger.exception``, so every routine dead-token refresh
   became a Sentry event. The async twin already branched correctly; the sync
   (mid-run, reactive) path never got the same treatment.
2. ``token_needs_refresh(None)`` returned False, so a token with unknown expiry
   was never proactively refreshed and ``providers_view`` reported it
   "connected" — with no validation call anywhere, ``expires_at`` being the only
   signal. A revoked-but-unexpired token therefore showed as healthy and was
   handed to the loaders, which then 401'd.
"""

from __future__ import annotations

import inspect
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from apps.common.error_codes import ErrorCode
from apps.users.services import token_refresh
from apps.users.services.token_refresh import (
    TokenRefreshError,
    TokenRefreshRejected,
    TokenRefreshUnavailable,
    refresh_oauth_token_sync,
)


def _social_token():
    token = Mock(token="old-access", app_id=1, account_id=1)
    token.app.client_id = "client-abc"
    token.app.secret = "shh"
    token.token_secret = "refresh-token"
    return token


def _mock_response(status: int, body: str):
    response = Mock()
    response.status_code = status
    response.text = body

    def _json():
        # requests raises on a non-JSON body; a bare Mock hands back a Mock, which
        # silently satisfies any assertion about the parsed payload.
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ValueError("no JSON object could be decoded") from exc

    response.json = _json
    return response


def _http_error(status: int, body: str) -> requests.HTTPError:
    response = _mock_response(status, body)
    error = requests.HTTPError(f"{status} error", response=response)
    response.raise_for_status.side_effect = error
    return error


def _post_returning(status: int, body: str):
    response = _mock_response(status, body)
    response.raise_for_status.side_effect = _http_error(status, body)
    return response


class TestSyncRefreshLogLevels:
    @pytest.fixture(autouse=True)
    def _isolate_persistence_for_http_unit_tests(self, mocker):
        mocker.patch(
            "apps.users.services.token_refresh._preflight_token",
            return_value=SimpleNamespace(
                refresh_token="refresh-token",
                expires_at=None,
            ),
        )
        mocker.patch("apps.users.services.token_refresh._record_refresh_failure")

    @pytest.mark.parametrize(
        ("error_code", "body_marker"),
        [
            ("invalid_grant", "dead-token-body-marker"),
            ("invalid_client", "bad-secret-body-marker"),
        ],
    )
    def test_4xx_logs_the_oauth_error_code_but_not_the_body(self, caplog, error_code, body_marker):
        """Both causes are warnings, but they must not read identically in the logs.

        invalid_client is our own misconfiguration and needs paging; invalid_grant is a
        routine reconnect. The OAuth error code is a fixed enum, so it is the signal
        that separates them -- while the provider's body still stays out.
        """
        caplog.set_level(logging.DEBUG)
        body = json.dumps({"error": error_code, "error_description": body_marker})
        with patch(
            "apps.users.services.token_refresh.requests.post",
            return_value=_post_returning(400, body),
        ):
            with pytest.raises(TokenRefreshError) as caught:
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
            rejected = error_code == "invalid_grant"
            assert caught.type is (TokenRefreshRejected if rejected else TokenRefreshError)
            assert caught.value.code == (
                ErrorCode.AUTH_TOKEN_EXPIRED if rejected else ErrorCode.AUTH_REFRESH_FAILED
            )

        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING record"
        message = warnings[0].getMessage()
        assert error_code in message, "the classified OAuth error code is the only signal"
        assert body_marker not in message, "the provider body must stay out of logs"
        assert body_marker not in caplog.text

    def test_an_unrecognised_error_code_is_withheld_rather_than_echoed(self, caplog):
        """Only the known OAuth enum is named; anything else could carry provider text."""
        caplog.set_level(logging.DEBUG)
        with patch(
            "apps.users.services.token_refresh.requests.post",
            return_value=_post_returning(400, '{"error": "surprise-leaky-value"}'),
        ):
            with pytest.raises(TokenRefreshError) as caught:
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
            assert caught.type is TokenRefreshError
            assert caught.value.code == ErrorCode.AUTH_REFRESH_FAILED

        assert "surprise-leaky-value" not in caplog.text
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING record"
        assert "other" in warnings[0].getMessage()

    @pytest.mark.parametrize("status", [400, 401, 403, 429])
    def test_all_4xx_are_warnings(self, caplog, status):
        caplog.set_level(logging.DEBUG)
        with patch(
            "apps.users.services.token_refresh.requests.post",
            return_value=_post_returning(status, "nope"),
        ):
            with pytest.raises(TokenRefreshError) as caught:
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
            retryable = status == 429
            assert caught.type is (TokenRefreshUnavailable if retryable else TokenRefreshError)
            assert caught.value.code == ErrorCode.AUTH_REFRESH_FAILED
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_still_reaches_sentry(self, caplog, status):
        """A provider's token endpoint 500ing is NOT expected — keep it loud."""
        caplog.set_level(logging.DEBUG)
        with patch(
            "apps.users.services.token_refresh.requests.post",
            return_value=_post_returning(status, "boom"),
        ):
            with pytest.raises(TokenRefreshError) as caught:
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
            assert caught.type is TokenRefreshUnavailable
            assert caught.value.code == ErrorCode.AUTH_REFRESH_FAILED
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, f"HTTP {status} must stay at ERROR"
        assert any(r.exc_info for r in errors)

    def test_non_http_failures_still_reach_sentry(self, caplog):
        """A connection error / bug in this code path is not an expected state."""
        caplog.set_level(logging.DEBUG)
        with patch(
            "apps.users.services.token_refresh.requests.post",
            side_effect=requests.ConnectionError("dns is down"),
        ):
            with pytest.raises(TokenRefreshError) as caught:
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
            assert caught.type is TokenRefreshUnavailable
            assert caught.value.code == ErrorCode.AUTH_REFRESH_FAILED
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors and any(r.exc_info for r in errors)

    def test_the_sync_path_matches_the_async_twin(self):
        """The defect was a divergence between the two, so pin them together."""
        # Both must branch on 4xx before falling through to logger.exception.
        for fn in (
            token_refresh.refresh_oauth_token_result,
            token_refresh.refresh_oauth_token_result_sync,
        ):
            body = inspect.getsource(fn)
            assert "400 <= " in body or "<= 499" in body or "< 500" in body, (
                f"{fn.__name__} must branch 4xx away from logger.exception"
            )
