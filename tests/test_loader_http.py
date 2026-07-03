"""Tests for the shared loader HTTP hardening (arch #252, finding 14#6)."""

from __future__ import annotations

import io

from urllib3.response import HTTPResponse

from mcp_server.loaders._http import (
    MAX_RETRY_AFTER_SECONDS,
    RETRY_STATUS_FORCELIST,
    RETRY_TOTAL,
    BoundedRetry,
    build_retry,
)


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
