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
import logging
from unittest.mock import Mock, patch

import pytest
import requests

from apps.users.services import token_refresh
from apps.users.services.token_refresh import TokenRefreshError, refresh_oauth_token_sync


def _social_token():
    token = Mock()
    token.app.client_id = "client-abc"
    token.app.secret = "shh"
    token.token_secret = "refresh-token"
    return token


def _http_error(status: int, body: str) -> requests.HTTPError:
    response = Mock()
    response.status_code = status
    response.text = body
    error = requests.HTTPError(f"{status} error", response=response)
    response.raise_for_status.side_effect = error
    return error


def _post_returning(status: int, body: str):
    response = Mock()
    response.status_code = status
    response.text = body
    response.raise_for_status.side_effect = _http_error(status, body)
    return response


class TestSyncRefreshLogLevels:
    def test_invalid_grant_is_a_warning_not_an_exception(self, caplog):
        """A dead refresh token is an expected outcome, not a bug."""
        caplog.set_level(logging.DEBUG)
        with patch(
            "apps.users.services.token_refresh.requests.post",
            return_value=_post_returning(400, '{"error": "invalid_grant"}'),
        ):
            with pytest.raises(TokenRefreshError):
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")

        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING record"
        # The body must survive so invalid_grant (dead token) can be told apart
        # from invalid_client (a misconfigured secret, which IS our bug).
        assert "invalid_grant" in warnings[0].getMessage()

    @pytest.mark.parametrize("status", [400, 401, 403, 429])
    def test_all_4xx_are_warnings(self, caplog, status):
        caplog.set_level(logging.DEBUG)
        with patch(
            "apps.users.services.token_refresh.requests.post",
            return_value=_post_returning(status, "nope"),
        ):
            with pytest.raises(TokenRefreshError):
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_still_reaches_sentry(self, caplog, status):
        """A provider's token endpoint 500ing is NOT expected — keep it loud."""
        caplog.set_level(logging.DEBUG)
        with patch(
            "apps.users.services.token_refresh.requests.post",
            return_value=_post_returning(status, "boom"),
        ):
            with pytest.raises(TokenRefreshError):
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
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
            with pytest.raises(TokenRefreshError):
                refresh_oauth_token_sync(_social_token(), "https://provider.test/o/token/")
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors and any(r.exc_info for r in errors)

    def test_the_sync_path_matches_the_async_twin(self):
        """The defect was a divergence between the two, so pin them together."""
        # Both must branch on 4xx before falling through to logger.exception.
        for fn in (token_refresh.refresh_oauth_token, token_refresh.refresh_oauth_token_sync):
            body = inspect.getsource(fn)
            assert "400 <= " in body or "<= 499" in body or "< 500" in body, (
                f"{fn.__name__} must branch 4xx away from logger.exception"
            )
