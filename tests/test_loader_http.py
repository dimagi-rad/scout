"""Tests for the shared loader HTTP hardening (arch #252, finding 14#6)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from urllib3.response import HTTPResponse

from mcp_server.loaders._http import (
    MAX_RETRY_AFTER_SECONDS,
    RETRY_STATUS_FORCELIST,
    RETRY_TOTAL,
    BoundedRetry,
    build_retry,
    get_with_auth_refresh,
)


def _fake_session(responses):
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = responses
    return session


def _response(headers: dict) -> HTTPResponse:
    return HTTPResponse(
        body=io.BytesIO(b""),
        headers=headers,
        status=429,
        version=11,
        version_string="HTTP/1.1",
        reason="",
        preload_content=False,
        decode_content=False,
    )


def test_build_retry_returns_bounded_retry():
    retry = build_retry()
    assert isinstance(retry, BoundedRetry)
    assert retry.total == RETRY_TOTAL
    assert retry.respect_retry_after_header is True
    assert 429 in RETRY_STATUS_FORCELIST


def test_retry_after_is_capped():
    retry = build_retry()
    # An upstream advertising a 10-minute Retry-After must not park the worker
    # for 10 minutes.
    assert retry.get_retry_after(_response({"Retry-After": "600"})) == MAX_RETRY_AFTER_SECONDS


def test_small_retry_after_passes_through():
    retry = build_retry()
    assert retry.get_retry_after(_response({"Retry-After": "5"})) == 5


def test_absent_retry_after_is_none():
    retry = build_retry()
    assert retry.get_retry_after(_response({})) is None


class TestGetWithAuthRefresh:
    def test_401_triggers_refresh_and_retries_once(self):
        resp401 = MagicMock(status_code=401)
        resp200 = MagicMock(status_code=200)
        session = _fake_session([resp401, resp200])
        refresh = MagicMock(return_value="new-token")

        result = get_with_auth_refresh(session, "https://x/y", refresh=refresh, timeout=(1, 1))

        assert result is resp200
        refresh.assert_called_once()
        assert session.get.call_count == 2
        assert session.headers["Authorization"] == "Bearer new-token"

    def test_no_refresh_when_none(self):
        resp401 = MagicMock(status_code=401)
        session = _fake_session([resp401])
        result = get_with_auth_refresh(session, "https://x/y", refresh=None, timeout=(1, 1))
        assert result is resp401
        assert session.get.call_count == 1

    def test_403_is_not_refreshed(self):
        resp403 = MagicMock(status_code=403)
        session = _fake_session([resp403])
        refresh = MagicMock(return_value="new-token")
        result = get_with_auth_refresh(session, "https://x/y", refresh=refresh, timeout=(1, 1))
        assert result is resp403
        refresh.assert_not_called()
        assert session.get.call_count == 1

    def test_refresh_failure_returns_original_401(self):
        resp401 = MagicMock(status_code=401)
        session = _fake_session([resp401])
        refresh = MagicMock(side_effect=RuntimeError("boom"))
        result = get_with_auth_refresh(session, "https://x/y", refresh=refresh, timeout=(1, 1))
        # Original 401 returned so the caller raises its provider AuthError.
        assert result is resp401
        assert session.get.call_count == 1

    def test_empty_new_token_does_not_retry(self):
        resp401 = MagicMock(status_code=401)
        session = _fake_session([resp401])
        refresh = MagicMock(return_value=None)
        result = get_with_auth_refresh(session, "https://x/y", refresh=refresh, timeout=(1, 1))
        assert result is resp401
        assert session.get.call_count == 1
