"""Tests for transient-overload handling in the chat SSE stream.

When Anthropic returns a transient capacity error (overloaded_error / rate
limit), the stream should signal a retryable error to the frontend and log at
WARNING (so it does not page via Sentry's ERROR-level capture) -- not surface a
generic "an error occurred" message or log an exception.
"""

import logging

import httpx
import pytest
from anthropic import APIStatusError, RateLimitError

from apps.chat import stream

STREAM_LOGGER = "apps.chat.stream"
GENERIC_ERROR_TEXT = "An error occurred while processing your request."


def _response(status_code: int) -> httpx.Response:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status_code, request=request)


def _overloaded_streaming_error() -> APIStatusError:
    """Mirror what anthropic raises on a mid-stream 'error' SSE event: the
    streaming response opened with status 200, so only the body carries the
    real error type."""
    body = {
        "type": "error",
        "error": {"details": None, "type": "overloaded_error", "message": "Overloaded"},
        "request_id": "req_011Cc7SPAEPXbj37vzLkjCoM",
    }
    return APIStatusError(f"{body}", response=_response(200), body=body)


class _RaisingAgent:
    """Stands in for the LangGraph agent; its event stream raises immediately."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def astream_events(self, input_state, *, config, version):
        exc = self._exc

        async def _gen():
            if False:  # make this an async generator without yielding
                yield
            raise exc

        return _gen()


async def _collect(exc: BaseException) -> list[str]:
    agent = _RaisingAgent(exc)
    return [chunk async for chunk in stream.langgraph_to_ui_stream(agent, {}, {"configurable": {}})]


# --- classifier ------------------------------------------------------------


def test_classifier_true_for_streaming_overloaded():
    assert stream._is_transient_overload(_overloaded_streaming_error()) is True


def test_classifier_true_for_rate_limit():
    assert (
        stream._is_transient_overload(RateLimitError("rate", response=_response(429), body=None))
        is True
    )


def test_classifier_false_for_bad_request():
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    exc = APIStatusError(f"{body}", response=_response(400), body=body)
    assert stream._is_transient_overload(exc) is False


def test_classifier_false_for_generic_exception():
    assert stream._is_transient_overload(ValueError("boom")) is False


# --- stream behavior -------------------------------------------------------


@pytest.mark.asyncio
async def test_overloaded_emits_retryable_signal_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger=STREAM_LOGGER):
        chunks = await _collect(_overloaded_streaming_error())
    joined = "".join(chunks)

    # Signals a retryable error the frontend can auto-retry on...
    assert "data-chat-status" in joined
    assert "retryable-error" in joined
    # ...instead of the generic dead-end error text.
    assert GENERIC_ERROR_TEXT not in joined
    # Stream still closes cleanly.
    assert any('"type": "finish"' in c for c in chunks)

    levels = {r.levelno for r in caplog.records if r.name == STREAM_LOGGER}
    assert logging.WARNING in levels  # logged...
    assert logging.ERROR not in levels  # ...but not at ERROR (no Sentry page)


@pytest.mark.asyncio
async def test_generic_error_still_reported_to_sentry(caplog):
    with caplog.at_level(logging.WARNING, logger=STREAM_LOGGER):
        chunks = await _collect(ValueError("boom"))
    joined = "".join(chunks)

    assert GENERIC_ERROR_TEXT in joined
    assert "retryable-error" not in joined
    # logger.exception -> ERROR record (Sentry captures this).
    assert any(r.levelno == logging.ERROR for r in caplog.records if r.name == STREAM_LOGGER)
