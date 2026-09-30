"""Tests for CubeClient's /v1/load handling (Continue wait long-polling)."""

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from apps.common.errors import ExpectedStateError
from apps.semantic.services import cube_client as cube_client_module
from apps.semantic.services import query
from apps.semantic.services.cube_client import CubeClient, CubeConnectionError, CubeQueryError


def _patched_async_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        cube_client_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )


@pytest.mark.asyncio
async def test_execute_query_retries_on_continue_wait(monkeypatch):
    calls = {"count": 0, "bodies": []}

    def handler(request):
        calls["count"] += 1
        calls["bodies"].append(json.loads(request.read()))
        if calls["count"] < 3:
            return httpx.Response(200, json={"error": "Continue wait"})
        return httpx.Response(
            200,
            json={
                "data": [{"visits.count": 3}],
                "annotation": {"measures": {"visits.count": {}}},
            },
        )

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "CONTINUE_WAIT_POLL_DELAY_SECONDS", 0)
    client = CubeClient(base_url="http://cube.test", api_secret="secret")

    result = await client.execute_query(
        {"measures": ["visits.count"]},
        security_context={"workspaceId": "w1"},
    )

    assert result == {"columns": ["visits.count"], "rows": [[3]], "row_count": 1}
    assert calls["count"] == 3
    # The same query is re-POSTed verbatim on every poll.
    assert all(body == {"query": {"measures": ["visits.count"]}} for body in calls["bodies"])


@pytest.mark.asyncio
async def test_execute_query_times_out_when_continue_wait_persists(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"error": "Continue wait"})

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "CONTINUE_WAIT_POLL_DELAY_SECONDS", 0)
    monkeypatch.setattr(cube_client_module, "QUERY_TOTAL_TIMEOUT_SECONDS", 0.0)
    client = CubeClient(base_url="http://cube.test", api_secret="secret")

    with pytest.raises(RuntimeError, match="timed out"):
        await client.execute_query(
            {"measures": ["visits.count"]},
            security_context={"workspaceId": "w1"},
        )


@pytest.mark.asyncio
async def test_execute_query_raises_on_real_cube_error(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"error": "Member not found: visits.bogus"})

    _patched_async_client(monkeypatch, handler)
    client = CubeClient(base_url="http://cube.test", api_secret="secret")

    with pytest.raises(CubeQueryError, match="Member not found"):
        await client.execute_query(
            {"measures": ["visits.bogus"]},
            security_context={"workspaceId": "w1"},
        )


@pytest.mark.asyncio
async def test_execute_query_raises_cube_query_error_from_http_error_payload(monkeypatch):
    def handler(request):
        return httpx.Response(400, json={"error": "Unsupported filter operator 'afterDate'."})

    _patched_async_client(monkeypatch, handler)
    client = CubeClient(base_url="http://cube.test", api_secret="secret")

    with pytest.raises(CubeQueryError, match="Unsupported filter operator"):
        await client.execute_query(
            {"measures": ["visits.count"]},
            security_context={"workspaceId": "w1"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "read", "cube_200", "cube_500", 429, 502, 503, 504])
async def test_transient_failures_recover_without_changing_request(monkeypatch, caplog, failure):
    requests = []

    def handler(request):
        requests.append((request.read(), request.headers["Authorization"]))
        if len(requests) == 1:
            if failure == "connect":
                raise httpx.ConnectError("offline", request=request)
            if failure == "read":
                raise httpx.ReadTimeout("timeout", request=request)
            if isinstance(failure, int):
                return httpx.Response(failure, text="temporarily unavailable")
            return httpx.Response(
                200 if failure == "cube_200" else 500,
                json={"error": "Error: Connection terminated due to connection timeout"},
            )
        return httpx.Response(200, json={"data": [{"visits.count": 7}]})

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "RETRY_BASE_DELAY_SECONDS", 0)
    with caplog.at_level(logging.INFO):
        result = await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {"measures": ["visits.count"]}, security_context={"workspaceId": "w1"}
        )
    assert result["rows"] == [[7]]
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert "recovered after 1 transient failure" in caplog.text
    assert "Authorization" not in caplog.text
    assert requests[0][1] not in caplog.text


@pytest.mark.asyncio
async def test_transient_retries_are_bounded_and_not_validation_errors(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"error": "connection terminated unexpectedly"})

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "RETRY_BASE_DELAY_SECONDS", 0)
    with pytest.raises(CubeConnectionError, match="temporarily unavailable"):
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={"workspaceId": "w1"}
        )
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 422, 500])
async def test_permanent_errors_are_never_retried(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "Member not found: visits.missing"})

    _patched_async_client(monkeypatch, handler)
    with pytest.raises(CubeQueryError, match="Member not found"):
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={}
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_permissions_fail_fast_even_with_transient_sounding_message(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"error": "connection refused"})

    _patched_async_client(monkeypatch, handler)
    with pytest.raises(CubeQueryError):
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={}
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_continue_wait_does_not_consume_transient_retry_budget(monkeypatch):
    responses = iter(
        [
            httpx.Response(503),
            *[httpx.Response(200, json={"error": "Continue wait"}) for _ in range(5)],
            httpx.Response(503),
            httpx.Response(200, json={"data": []}),
        ]
    )
    _patched_async_client(monkeypatch, lambda request: next(responses))
    monkeypatch.setattr(cube_client_module, "RETRY_BASE_DELAY_SECONDS", 0)
    monkeypatch.setattr(cube_client_module, "CONTINUE_WAIT_POLL_DELAY_SECONDS", 0)
    result = await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
        {}, security_context={}
    )
    assert result["rows"] == []
    assert next(responses, None) is None


@pytest.mark.asyncio
async def test_overall_deadline_bounds_in_flight_http_request(monkeypatch):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json={"data": []})

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "QUERY_TOTAL_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(CubeConnectionError, match="timed out"):
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={}
        )
    assert entered.is_set()


@pytest.mark.asyncio
async def test_retry_after_cannot_extend_deadline(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "QUERY_TOTAL_TIMEOUT_SECONDS", 1.0)
    # A regression must fail immediately, never sleep for two minutes in CI.
    monkeypatch.setattr(
        cube_client_module.asyncio,
        "sleep",
        AsyncMock(side_effect=AssertionError("Unexpected backoff sleep")),
    )
    with pytest.raises(CubeConnectionError, match="retry delay exceeds"):
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={}
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_is_not_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise asyncio.CancelledError

    _patched_async_client(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={}
        )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["<html>Gateway failure</html>", "{}", "[]"])
async def test_malformed_success_body_is_a_connection_error(monkeypatch, body):
    _patched_async_client(monkeypatch, lambda request: httpx.Response(200, text=body))
    with pytest.raises(CubeConnectionError, match="invalid query response"):
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={}
        )


@pytest.mark.asyncio
async def test_exhausted_transport_error_keeps_its_cause(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("upstream timed out", request=request)

    _patched_async_client(monkeypatch, handler)
    monkeypatch.setattr(cube_client_module, "RETRY_BASE_DELAY_SECONDS", 0)
    with pytest.raises(CubeConnectionError) as raised:
        await CubeClient(base_url="http://cube.test", api_secret="secret").execute_query(
            {}, security_context={}
        )
    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (CubeConnectionError("temporary"), "CONNECTION_ERROR"),
        (CubeQueryError("invalid member"), "VALIDATION_ERROR"),
    ],
)
async def test_retry_exhaustion_is_reported_as_connection_not_validation(
    monkeypatch, failure, code
):

    monkeypatch.setattr(
        query,
        "_compile_semantic_query_for_async",
        lambda *args: {
            "cube_query": {},
            "model": None,
            "cube_schema": None,
        },
    )
    monkeypatch.setattr(query, "load_workspace_context", AsyncMock(return_value=None))
    monkeypatch.setattr(query, "build_cube_security_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(CubeClient, "execute_query", AsyncMock(side_effect=failure))
    result = await query.run_semantic_query(SimpleNamespace(id="workspace"), {})
    assert result["success"] is False
    assert result["error"]["code"] == code
    assert str(failure) in result["error"]["message"]


def _schema_operation(operation):
    client = CubeClient(base_url="http://cube.test", api_secret="secret")
    if operation == "validate":
        return client.validate_schema("cubes: []")
    return client.invalidate_schema_cache(security_context={"workspaceId": "w1"})


@pytest.fixture
def validator_url(settings, monkeypatch):
    settings.CUBE_VALIDATOR_URL = "http://validator.test"
    monkeypatch.setattr(cube_client_module, "SCHEMA_RETRY_BASE_DELAY_SECONDS", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read_timeout", "disconnect", "connect", 502, 503, 504])
async def test_validation_retries_transient_failures_then_succeeds(
    monkeypatch, caplog, validator_url, failure
):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "read_timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if failure == "disconnect":
                raise httpx.RemoteProtocolError(
                    "Server disconnected without sending a response.", request=request
                )
            if failure == "connect":
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(failure, json={"valid": False, "errors": ["service error"]})
        return httpx.Response(200, json={"valid": True, "errors": []})

    _patched_async_client(monkeypatch, handler)
    with caplog.at_level(logging.INFO, logger=cube_client_module.__name__):
        await _schema_operation("validate")

    assert len(calls) == 2
    assert calls[0].url == calls[1].url
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
# Cube finishes a /meta compile after the client leaves, so the warm-up never retries.
@pytest.mark.parametrize(("operation", "attempts"), [("validate", 3), ("invalidate", 1)])
@pytest.mark.parametrize("failure", ["read_timeout", "disconnect", 503])
async def test_schema_operations_give_up_after_bounded_attempts(
    monkeypatch, caplog, validator_url, operation, attempts, failure
):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "read_timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "disconnect":
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(503, json={"valid": False})

    _patched_async_client(monkeypatch, handler)
    with (
        caplog.at_level(logging.INFO, logger=cube_client_module.__name__),
        pytest.raises(CubeConnectionError) as raised,
    ):
        await _schema_operation(operation)

    assert len(calls) == attempts
    assert isinstance(raised.value, ExpectedStateError)
    assert raised.value.__cause__ is not None
    # The caller owns the single WARNING; retries themselves stay below Sentry.
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["validate", "invalidate"])
@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_schema_operations_do_not_retry_permanent_errors(
    monkeypatch, validator_url, operation, status
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "nope"})

    _patched_async_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await _schema_operation(operation)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["validate", "invalidate"])
async def test_schema_operations_fail_fast_when_cube_is_unreachable(
    monkeypatch, validator_url, operation
):
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"valid": True})

    _patched_async_client(monkeypatch, handler)
    await _schema_operation(operation)

    assert timeouts[0]["connect"] <= 10
    assert timeouts[0]["read"] is not None


@pytest.mark.asyncio
async def test_slow_validator_failure_is_not_retried_past_the_budget(monkeypatch, validator_url):
    monkeypatch.setattr(cube_client_module, "VALIDATE_BUDGET_SECONDS", 5.2)
    read_timeouts = []

    def handler(request):
        read_timeouts.append(request.extensions["timeout"]["read"])
        time.sleep(0.3)
        return httpx.Response(503, json={"valid": False})

    _patched_async_client(monkeypatch, handler)
    with pytest.raises(cube_client_module.CubeServiceUnavailable, match="after 1 attempt"):
        await _schema_operation("validate")

    assert len(read_timeouts) == 1
    assert read_timeouts[0] <= 5.2


@pytest.mark.asyncio
async def test_exhaustion_reports_the_503_not_a_later_shortened_timeout(
    monkeypatch, caplog, validator_url
):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"valid": False})
        raise httpx.ReadTimeout("timed out", request=request)

    _patched_async_client(monkeypatch, handler)
    with (
        caplog.at_level(logging.INFO, logger=cube_client_module.__name__),
        pytest.raises(cube_client_module.CubeServiceUnavailable, match="HTTP 503"),
    ):
        await _schema_operation("validate")
    assert len(calls) == 3
    # Each retry line names what that attempt actually hit.
    assert "(ReadTimeout), retry 2/2" in caplog.text


@pytest.mark.asyncio
async def test_exhaustion_reports_a_later_connect_failure_over_an_earlier_503(
    monkeypatch, validator_url
):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"valid": False})
        raise httpx.ConnectTimeout("validator gone", request=request)

    _patched_async_client(monkeypatch, handler)
    with pytest.raises(cube_client_module.CubeServiceUnavailable, match="ConnectTimeout"):
        await _schema_operation("validate")
    assert len(calls) == 3
