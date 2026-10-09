from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import pytest
from asgiref.sync import async_to_sync

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection
from apps.users.services.access_verification_providers import (
    CONNECT_LIGHT_CONCURRENCY,
    ProcessNetworkLimiter,
    verify_provider,
)
from apps.users.services.access_verification_types import (
    CredentialObservation,
    CredentialRequestSnapshot,
    VerificationOutcome,
)
from apps.users.services.tenant_listing.connect import MAX_SUBSET


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response(status=200, payload=None, *, url="https://provider.example/list/"):
    request = httpx.Request("GET", url)
    if payload is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, json=payload, request=request)


def _request(provider, credential_type=TenantConnection.OAUTH, credential="secret"):
    return CredentialRequestSnapshot(
        CredentialObservation(
            connection_id=uuid4(),
            user_id=1,
            provider=provider,
            credential_type=credential_type,
            credential_fingerprint="fingerprint",
            account_identity="account" if credential_type == TenantConnection.OAUTH else "",
            scope_key="team" if provider == "ocs" else "",
            upstream_denied_at=None,
        ),
        credential,
    )


async def _verify(
    request, responses, *, settings, deadline=100.0, clock=lambda: 0.0, external_ids=frozenset()
):
    client = _Client(responses)
    result = await verify_provider(
        request,
        deadline=deadline,
        clock=clock,
        client_factory=lambda: client,
        settings=settings,
        limiter=asyncio.Semaphore(1),
        external_ids=frozenset(external_ids),
    )
    return result, client.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "credential_type", "credential", "payload", "expected_url", "header"),
    [
        (
            "commcare",
            TenantConnection.OAUTH,
            "oauth-token",
            {"objects": [{"domain_name": "a", "project_name": "A"}], "meta": {"next": None}},
            "https://www.commcarehq.org/api/user_domains/v1/",
            ("Authorization", "Bearer oauth-token"),
        ),
        (
            "commcare",
            TenantConnection.API_KEY,
            "person@example.com:key:with:colons",
            {"objects": [{"domain_name": "a", "project_name": "A"}], "meta": {"next": None}},
            "https://www.commcarehq.org/api/user_domains/v1/",
            ("Authorization", "ApiKey person@example.com:key:with:colons"),
        ),
        (
            "ocs",
            TenantConnection.OAUTH,
            "oauth-token",
            {"results": [{"id": "bot", "name": "Bot"}], "next": None},
            "https://ocs.example/api/experiments/",
            ("Authorization", "Bearer oauth-token"),
        ),
        (
            "ocs",
            TenantConnection.API_KEY,
            "api-key",
            {"results": [{"id": "bot", "name": "Bot"}], "next": None},
            "https://ocs.example/api/experiments/",
            ("X-api-key", "api-key"),
        ),
        (
            "commcare_connect",
            TenantConnection.OAUTH,
            "oauth-token",
            {"opportunities": [{"id": 7, "name": "Seven"}]},
            "https://connect.example/export/opp_org_program_list/",
            ("Authorization", "Bearer oauth-token"),
        ),
    ],
)
async def test_provider_protocols_return_complete_external_ids(
    settings, provider, credential_type, credential, payload, expected_url, header
):
    settings.OCS_URL = "https://ocs.example"
    settings.CONNECT_API_URL = "https://connect.example"

    result, requests = await _verify(
        _request(provider, credential_type, credential),
        [_response(payload=payload)],
        settings=settings,
    )

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset(
        {"a" if provider == "commcare" else "bot" if provider == "ocs" else "7"}
    )
    assert requests[0][0] == expected_url
    assert requests[0][1]["headers"][header[0]] == header[1]
    assert requests[0][1]["follow_redirects"] is False
    assert 0 < requests[0][1]["timeout"] <= 10


@pytest.mark.asyncio
async def test_complete_pagination_accepts_relative_and_same_origin_absolute_urls(settings):
    settings.OCS_URL = "https://ocs.example"
    responses = [
        _response(payload={"results": [{"id": "one", "name": "One"}], "next": "?page=2"}),
        _response(
            payload={
                "results": [{"id": "two", "name": "Two"}],
                "next": "https://ocs.example/api/experiments/?page=3",
            }
        ),
        _response(payload={"results": [], "next": None}),
    ]

    result, requests = await _verify(_request("ocs"), responses, settings=settings)

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset({"one", "two"})
    assert [request[0] for request in requests] == [
        "https://ocs.example/api/experiments/",
        "https://ocs.example/api/experiments/?page=2",
        "https://ocs.example/api/experiments/?page=3",
    ]


@pytest.mark.asyncio
async def test_commcare_complete_pagination_follows_meta_next(settings):
    responses = [
        _response(
            payload={
                "objects": [{"domain_name": "one", "project_name": "One"}],
                "meta": {"next": "/api/user_domains/v1/?offset=1"},
            }
        ),
        _response(
            payload={
                "objects": [{"domain_name": "two", "project_name": "Two"}],
                "meta": {"next": None},
            }
        ),
    ]

    result, requests = await _verify(_request("commcare"), responses, settings=settings)

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset({"one", "two"})
    assert requests[1][0] == "https://www.commcarehq.org/api/user_domains/v1/?offset=1"


@pytest.mark.asyncio
async def test_commcare_unpaginated_listing_is_complete(settings):
    # HQ's DoesNothingPaginator: total_count and no next key (#880).
    payload = {
        "objects": [
            {"domain_name": "one", "project_name": "One"},
            {"domain_name": "two", "project_name": "Two"},
        ],
        "meta": {"total_count": 2},
    }

    result, requests = await _verify(
        _request("commcare"), [_response(payload=payload)], settings=settings
    )

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset({"one", "two"})
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_commcare_listing_short_of_its_total_count_is_indeterminate(settings, caplog):
    payload = {
        "objects": [{"domain_name": "one", "project_name": "One"}],
        "meta": {"total_count": 2},
    }

    with caplog.at_level("WARNING", logger="apps.users.services.access_verification_providers"):
        result, _ = await _verify(
            _request("commcare"), [_response(payload=payload)], settings=settings
        )

    assert result.outcome == VerificationOutcome.INDETERMINATE
    assert any(
        "indeterminate" in r.getMessage() and "cause=next_undeclared" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_empty_complete_response_is_success(settings):
    settings.OCS_URL = "https://ocs.example"
    result, _ = await _verify(
        _request("ocs"), [_response(payload={"results": [], "next": None})], settings=settings
    )
    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retryable_http_status_is_unavailable(settings, status):
    settings.OCS_URL = "https://ocs.example"
    result, _ = await _verify(_request("ocs"), [_response(status)], settings=settings)
    assert result.outcome == VerificationOutcome.UNAVAILABLE


@pytest.mark.asyncio
async def test_transport_and_deadline_are_unavailable(settings):
    settings.OCS_URL = "https://ocs.example"
    result, _ = await _verify(_request("ocs"), [httpx.ConnectError("offline")], settings=settings)
    assert result.outcome == VerificationOutcome.UNAVAILABLE

    result, requests = await _verify(
        _request("ocs"), [_response(payload={})], settings=settings, deadline=0.0
    )
    assert result.outcome == VerificationOutcome.UNAVAILABLE
    assert requests == []


@pytest.mark.asyncio
async def test_adapter_enforces_wall_clock_deadline_when_client_ignores_timeout(settings):
    settings.OCS_URL = "https://ocs.example"

    class HangingClient(_Client):
        async def get(self, url, **kwargs):
            await asyncio.Event().wait()

    client = HangingClient([])
    result = await asyncio.wait_for(
        verify_provider(
            _request("ocs"),
            deadline=asyncio.get_running_loop().time() + 0.05,
            clock=asyncio.get_running_loop().time,
            client_factory=lambda: client,
            settings=settings,
            limiter=asyncio.Semaphore(1),
        ),
        timeout=0.5,
    )

    assert result.outcome == VerificationOutcome.UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["commcare", "ocs", "commcare_connect"])
@pytest.mark.parametrize("status", [401, 403])
async def test_401_is_credential_rejection_and_collection_403_is_indeterminate(
    settings, provider, status
):
    settings.OCS_URL = "https://ocs.example"
    settings.CONNECT_API_URL = "https://connect.example"
    result, _ = await _verify(_request(provider), [_response(status)], settings=settings)
    if status == 401:
        assert result.outcome == VerificationOutcome.CREDENTIAL_REJECTED
        assert result.error_code == ErrorCode.AUTH_TOKEN_EXPIRED
    else:
        assert result.outcome == VerificationOutcome.INDETERMINATE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "logged"),
    [
        ('Bearer realm="api",error="invalid_token"', "(invalid_token)"),
        ('Bearer realm="api"', "(no token error)"),
    ],
)
async def test_401_logs_whether_the_provider_blamed_the_token(settings, caplog, header, logged):
    settings.OCS_URL = "https://ocs.example"
    response = httpx.Response(
        401,
        headers={"WWW-Authenticate": header},
        request=httpx.Request("GET", "https://ocs.example/api/experiments/"),
    )
    with caplog.at_level("INFO", logger="apps.users.services.access_verification_providers"):
        result, _ = await _verify(_request("ocs"), [response], settings=settings)

    assert result.outcome == VerificationOutcome.CREDENTIAL_REJECTED
    assert any(logged in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": {}, "next": None},
        {"results": [{"name": "missing id"}], "next": None},
        {"results": [{"id": "same", "name": "A"}, {"id": "same", "name": "B"}], "next": None},
    ],
)
async def test_malformed_ocs_data_is_indeterminate(settings, payload):
    settings.OCS_URL = "https://ocs.example"
    result, _ = await _verify(_request("ocs"), [_response(payload=payload)], settings=settings)
    assert result.outcome == VerificationOutcome.INDETERMINATE


@pytest.mark.asyncio
async def test_page_two_failure_discards_partial_results(settings):
    settings.OCS_URL = "https://ocs.example"
    result, _ = await _verify(
        _request("ocs"),
        [
            _response(payload={"results": [{"id": "one"}], "next": "?page=2"}),
            _response(503),
        ],
        settings=settings,
    )
    assert result.outcome == VerificationOutcome.UNAVAILABLE
    assert result.external_ids == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/steal",
        "/api/experiments/",
    ],
)
async def test_unsafe_or_repeated_pagination_is_indeterminate(settings, next_url):
    settings.OCS_URL = "https://ocs.example"
    result, requests = await _verify(
        _request("ocs"),
        [_response(payload={"results": [], "next": next_url})],
        settings=settings,
    )
    assert result.outcome == VerificationOutcome.INDETERMINATE
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_redirect_is_indeterminate_and_is_not_followed(settings):
    settings.OCS_URL = "https://ocs.example"
    result, requests = await _verify(_request("ocs"), [_response(302)], settings=settings)
    assert result.outcome == VerificationOutcome.INDETERMINATE
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_connect_rejects_pagination_indicators(settings):
    settings.CONNECT_API_URL = "https://connect.example"
    result, _ = await _verify(
        _request("commcare_connect"),
        [_response(payload={"opportunities": [], "next": "/page/2"})],
        settings=settings,
    )
    assert result.outcome == VerificationOutcome.INDETERMINATE


@pytest.mark.asyncio
async def test_bounds_reject_page_101_and_row_10001(settings):
    settings.OCS_URL = "https://ocs.example"
    page_responses = [
        _response(payload={"results": [], "next": f"?page={page + 2}"}) for page in range(100)
    ]
    result, requests = await _verify(_request("ocs"), page_responses, settings=settings)
    assert result.outcome == VerificationOutcome.INDETERMINATE
    assert len(requests) == 100

    result, _ = await _verify(
        _request("ocs"),
        [_response(payload={"results": [{"id": str(i)} for i in range(10001)], "next": None})],
        settings=settings,
    )
    assert result.outcome == VerificationOutcome.INDETERMINATE


@pytest.mark.asyncio
async def test_invalid_commcare_api_key_shape_is_indeterminate_without_network(settings):
    request = replace(_request("commcare", TenantConnection.API_KEY), credential="no-colon")
    result, requests = await _verify(request, [], settings=settings)
    assert result.outcome == VerificationOutcome.INDETERMINATE
    assert requests == []


def test_default_process_limiter_survives_successive_contended_event_loops(settings):
    settings.OCS_URL = "https://ocs.example"

    async def burst():
        active = 0
        peak = 0

        class DelayedClient(_Client):
            async def get(self, url, **kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.01)
                    return _response(payload={"results": [], "next": None})
                finally:
                    active -= 1

        results = await asyncio.gather(
            *(
                verify_provider(
                    _request("ocs"),
                    settings=settings,
                    client_factory=lambda: DelayedClient([]),
                    deadline=asyncio.get_running_loop().time() + 1,
                )
                for _ in range(8)
            )
        )
        assert all(result.outcome == VerificationOutcome.COMPLETE for result in results)
        # Upper bound only: requiring exactly 4 would need all four to reach
        # DelayedClient.get inside the 10ms window. Without the limiter this
        # would be 8.
        assert peak <= 4

    async_to_sync(burst)()
    async_to_sync(burst)()


def test_default_limiter_bounds_concurrent_loops_process_wide(settings):
    settings.OCS_URL = "https://ocs.example"
    start = Barrier(2)
    lock = Lock()
    active = 0
    peak = 0

    class DelayedClient(_Client):
        async def get(self, url, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                return _response(payload={"results": [], "next": None})
            finally:
                with lock:
                    active -= 1

    async def burst():
        return await asyncio.gather(
            *(
                verify_provider(
                    _request("ocs"),
                    settings=settings,
                    client_factory=lambda: DelayedClient([]),
                    deadline=asyncio.get_running_loop().time() + 2,
                )
                for _ in range(8)
            )
        )

    def run():
        start.wait(timeout=2)
        return asyncio.run(burst())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        for future in futures:
            assert all(r.outcome == VerificationOutcome.COMPLETE for r in future.result(timeout=3))
    # Two OS threads under the GIL: a >20ms preemption leaves fewer than four
    # simultaneously active. Without the limiter this would reach 16.
    assert peak <= 4
    assert active == 0


@pytest.mark.asyncio
async def test_limiter_wait_deadline_does_not_start_network_or_leak_permit(settings):
    settings.OCS_URL = "https://ocs.example"
    limiter = ProcessNetworkLimiter(1)
    await limiter.acquire()
    factory = Mock()
    result = await verify_provider(
        _request("ocs"),
        settings=settings,
        limiter=limiter,
        client_factory=factory,
        deadline=asyncio.get_running_loop().time() + 0.03,
    )
    assert result.outcome == VerificationOutcome.UNAVAILABLE
    factory.assert_not_called()
    limiter.release()
    assert await asyncio.wait_for(limiter.acquire(), timeout=0.1)
    limiter.release()
    with pytest.raises(ValueError):
        limiter.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", [False, True])
async def test_limiter_cancellation_preserves_capacity(settings, waiting):
    settings.OCS_URL = "https://ocs.example"
    limiter = ProcessNetworkLimiter(1)
    network_started = asyncio.Event()
    network_stopped = asyncio.Event()

    class HangingClient(_Client):
        async def get(self, url, **kwargs):
            network_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                network_stopped.set()

    if waiting:
        await limiter.acquire()
    task = asyncio.create_task(
        verify_provider(
            _request("ocs"),
            settings=settings,
            limiter=limiter,
            client_factory=lambda: HangingClient([]),
        )
    )
    if waiting:
        await asyncio.sleep(0.02)
        assert not network_started.is_set()
    else:
        await asyncio.wait_for(network_started.wait(), timeout=0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if waiting:
        limiter.release()
    else:
        assert network_stopped.is_set()
    assert await asyncio.wait_for(limiter.acquire(), timeout=0.1)
    limiter.release()
    with pytest.raises(ValueError):
        limiter.release()


@pytest.mark.asyncio
async def test_permit_is_released_when_the_wait_is_cut_short_after_acquiring(settings):
    """A permit taken just as the wait is cut short must not be lost.

    stdlib wait_for rescues this today -- 3.11 returns the inner future's result
    when the outer wait is cancelled, 3.12+ runs the coroutine inline so there is
    no window -- but the rescue is a version-specific implementation detail. This
    drives the unsafe interleaving directly: the waiter completes and takes the
    permit, then the wait reports cancellation anyway. Without the guard in
    verify_provider the permit is never released and the process-wide limiter
    loses capacity permanently.
    """
    settings.OCS_URL = "https://ocs.example"
    limiter = ProcessNetworkLimiter(1)
    factory = Mock()
    real_wait_for = asyncio.wait_for

    # Drop-in for asyncio.wait_for, so it must mirror that signature.
    async def cut_short_after_success(fut, timeout=None):  # noqa: ASYNC109
        await real_wait_for(fut, timeout=timeout)
        raise asyncio.CancelledError

    with patch.object(asyncio, "wait_for", cut_short_after_success):
        with pytest.raises(asyncio.CancelledError):
            await verify_provider(
                _request("ocs"),
                settings=settings,
                limiter=limiter,
                client_factory=factory,
            )

    factory.assert_not_called()
    # Capacity fully restored, and restored exactly once: a leaked permit makes
    # the acquire below starve, while a double release raises ValueError.
    assert await real_wait_for(limiter.acquire(), timeout=0.1)
    limiter.release()
    with pytest.raises(ValueError):
        limiter.release()


@pytest.mark.asyncio
async def test_ocs_oauth_without_scope_is_indeterminate_without_network(settings):
    snapshot = _request("ocs")
    snapshot = replace(snapshot, observation=replace(snapshot.observation, scope_key=""))
    result, requests = await _verify(snapshot, [], settings=settings)
    assert result.outcome == VerificationOutcome.INDETERMINATE
    assert requests == []


@pytest.mark.asyncio
async def test_exact_row_bound_is_complete(settings):
    settings.OCS_URL = "https://ocs.example"
    result, _ = await _verify(
        _request("ocs"),
        [_response(payload={"results": [{"id": str(i)} for i in range(10000)], "next": None})],
        settings=settings,
    )
    assert result.outcome == VerificationOutcome.COMPLETE
    assert len(result.external_ids) == 10000


_PROVIDERS_LOGGER = "apps.users.services.access_verification_providers"


def _unconfirmed_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name == _PROVIDERS_LOGGER
        and record.getMessage().startswith("Upstream access verification ")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "deadline", "cause", "status"),
    [
        ([_response(503)], 100.0, "cause=http_status", "status=503"),
        ([httpx.ConnectError("offline")], 100.0, "cause=request_error:ConnectError", "status=-"),
        ([httpx.ReadTimeout("slow")], 100.0, "cause=request_error:ReadTimeout", "status=-"),
        ([_response(payload={})], 0.0, "cause=deadline_before_start", "status=-"),
    ],
)
async def test_unavailable_logs_cause_at_warning_without_secrets(
    settings, caplog, responses, deadline, cause, status
):
    settings.OCS_URL = "https://ocs.example"
    request = _request("ocs", credential="super-secret-token")
    caplog.set_level("WARNING", logger=_PROVIDERS_LOGGER)

    result, _ = await _verify(request, responses, settings=settings, deadline=deadline)

    assert result.outcome == VerificationOutcome.UNAVAILABLE
    [record] = _unconfirmed_records(caplog)
    assert record.levelname == "WARNING"
    message = record.getMessage()
    assert "provider=ocs" in message
    assert cause in message
    assert status in message
    assert "elapsed_ms=" in message
    assert f"connection_id={request.observation.connection_id}" in message
    assert "user_id=1" in message
    assert "budget_ms=" in message
    assert "super-secret-token" not in message
    assert "ocs.example" not in message


@pytest.mark.asyncio
async def test_unavailable_log_reports_request_timeout_and_elapsed_ms(settings, caplog):
    settings.OCS_URL = "https://ocs.example"
    caplog.set_level("WARNING", logger=_PROVIDERS_LOGGER)

    class HangingClient(_Client):
        async def get(self, url, **kwargs):
            await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    result = await verify_provider(
        _request("ocs"),
        deadline=loop.time() + 0.05,
        clock=loop.time,
        client_factory=lambda: HangingClient([]),
        settings=settings,
        limiter=asyncio.Semaphore(1),
    )

    assert result.outcome == VerificationOutcome.UNAVAILABLE
    [record] = _unconfirmed_records(caplog)
    message = record.getMessage()
    assert "cause=request_timeout" in message
    assert "page=1" in message
    elapsed_ms = int(message.split("elapsed_ms=")[1].split()[0])
    assert elapsed_ms >= 40


@pytest.mark.asyncio
async def test_unavailable_log_reports_limiter_wait_timeout(settings, caplog):
    settings.OCS_URL = "https://ocs.example"
    caplog.set_level("WARNING", logger=_PROVIDERS_LOGGER)
    limiter = ProcessNetworkLimiter(1)
    await limiter.acquire()
    try:
        result = await verify_provider(
            _request("ocs"),
            settings=settings,
            limiter=limiter,
            client_factory=Mock(),
            deadline=asyncio.get_running_loop().time() + 0.03,
        )
    finally:
        limiter.release()

    assert result.outcome == VerificationOutcome.UNAVAILABLE
    [record] = _unconfirmed_records(caplog)
    assert "cause=limiter_wait_timeout" in record.getMessage()


@pytest.mark.asyncio
async def test_non_unavailable_outcomes_do_not_log_unavailable(settings, caplog):
    settings.OCS_URL = "https://ocs.example"
    caplog.set_level("WARNING", logger=_PROVIDERS_LOGGER)

    rejected, _ = await _verify(_request("ocs"), [_response(401)], settings=settings)
    indeterminate, _ = await _verify(_request("ocs"), [_response(403)], settings=settings)

    assert rejected.outcome != VerificationOutcome.UNAVAILABLE
    assert indeterminate.outcome != VerificationOutcome.UNAVAILABLE
    [record] = _unconfirmed_records(caplog)
    assert record.getMessage().startswith("Upstream access verification indeterminate")
    assert "status=403" in record.getMessage()


@pytest.mark.asyncio
async def test_cancellation_by_the_caller_is_logged_and_propagates(settings, caplog):
    settings.OCS_URL = "https://ocs.example"
    caplog.set_level("WARNING", logger=_PROVIDERS_LOGGER)
    started = asyncio.Event()

    class HangingClient(_Client):
        async def get(self, url, **kwargs):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.ensure_future(
        verify_provider(
            _request("ocs"),
            deadline=100.0,
            clock=lambda: 0.0,
            client_factory=lambda: HangingClient([]),
            settings=settings,
            limiter=asyncio.Semaphore(1),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [record] = _unconfirmed_records(caplog)
    assert record.getMessage().startswith("Upstream access verification cancelled")
    assert "cause=cancelled" in record.getMessage()
    assert "page=1" in record.getMessage()


@pytest.mark.asyncio
async def test_unavailable_log_reports_a_response_past_the_deadline(settings, caplog):
    settings.OCS_URL = "https://ocs.example"
    caplog.set_level("WARNING", logger=_PROVIDERS_LOGGER)
    now = [0.0]

    class SlowClient(_Client):
        async def get(self, url, **kwargs):
            now[0] = 6.0
            return await super().get(url, **kwargs)

    client = SlowClient([_response(payload={"results": [], "next": None})])
    result = await verify_provider(
        _request("ocs"),
        deadline=5.0,
        clock=lambda: now[0],
        client_factory=lambda: client,
        settings=settings,
        limiter=asyncio.Semaphore(1),
    )

    assert result.outcome == VerificationOutcome.UNAVAILABLE
    [record] = _unconfirmed_records(caplog)
    message = record.getMessage()
    assert "cause=deadline_after_response" in message
    assert "status=200" in message
    assert "elapsed_ms=6000" in message
    assert "budget_ms=5000" in message


def _connect_404(*, json_body=True):
    request = httpx.Request("GET", "https://connect.example/export/opportunity/7/")
    if json_body:
        return httpx.Response(404, json={"detail": "Not found."}, request=request)
    return httpx.Response(
        404,
        text="<!DOCTYPE html><html>Page not found</html>",
        headers={"content-type": "text/html; charset=utf-8"},
        request=request,
    )


@pytest.mark.asyncio
async def test_connect_checks_only_the_requested_opportunities(settings):
    settings.CONNECT_API_URL = "https://connect.example"
    result, requests = await _verify(
        _request("commcare_connect"),
        [_response(payload={"id": 7, "name": "Seven"}), _response(payload={"id": 8})],
        settings=settings,
        external_ids={"8", "7"},
    )

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.scoped is True
    assert result.external_ids == frozenset({"7", "8"})
    assert [url for url, _kwargs in requests] == [
        "https://connect.example/export/opportunity/7/",
        "https://connect.example/export/opportunity/8/",
    ]
    assert requests[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert requests[0][1]["follow_redirects"] is False


@pytest.mark.asyncio
async def test_connect_no_access_404_omits_that_opportunity_and_checks_the_rest(settings):
    settings.CONNECT_API_URL = "https://connect.example"
    result, requests = await _verify(
        _request("commcare_connect"),
        [_response(payload={"id": 3}), _connect_404(), _response(payload={"id": 9})],
        settings=settings,
        external_ids={"3", "7", "9"},
    )

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset({"3", "9"})
    assert result.scope == frozenset({"3", "7", "9"})
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "later",
    [
        pytest.param(_response(503), id="5xx"),
        pytest.param(httpx.ConnectError("down"), id="network"),
        pytest.param(_response(403), id="indeterminate"),
        pytest.param(_response(payload={"id": 99}), id="needs-listing"),
    ],
)
async def test_connect_failure_after_a_404_keeps_the_revocation(settings, later, caplog):
    settings.CONNECT_API_URL = "https://connect.example"
    caplog.set_level(logging.INFO, logger="apps.users.services.access_verification_providers")
    result, requests = await _verify(
        _request("commcare_connect"),
        [_response(payload={"id": 3}), _connect_404(), later],
        settings=settings,
        external_ids={"3", "7", "9"},
    )

    # What was decided stands, scoped to it; 9 was not answered, so is not covered.
    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset({"3"})
    assert result.scope == frozenset({"3", "7"})
    assert len(requests) == 3
    # Logged as what it was, a partial result, never as an unavailable attempt.
    messages = [record.getMessage() for record in caplog.records]
    assert any("settled_after_omission" in message for message in messages)
    assert not any("verification unavailable" in message for message in messages)


@pytest.mark.asyncio
async def test_connect_401_after_a_404_is_still_a_credential_rejection(settings):
    settings.CONNECT_API_URL = "https://connect.example"
    result, _requests = await _verify(
        _request("commcare_connect"),
        [_connect_404(), _response(401)],
        settings=settings,
        external_ids={"7", "9"},
    )

    assert result.outcome == VerificationOutcome.CREDENTIAL_REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_response(403, payload={"detail": "scope"}), id="403-missing-scope"),
        pytest.param(_response(302), id="redirect"),
    ],
)
async def test_connect_unrecognized_answer_is_indeterminate_not_denial(settings, response):
    settings.CONNECT_API_URL = "https://connect.example"
    result, _ = await _verify(
        _request("commcare_connect"), [response], settings=settings, external_ids={"7"}
    )
    assert result.outcome == VerificationOutcome.INDETERMINATE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        pytest.param(_response(payload={"id": 8}), id="200-other-opportunity"),
        pytest.param(_response(payload={"name": "no id"}), id="200-no-id"),
        pytest.param(_response(payload=[{"id": 7}]), id="200-list"),
        pytest.param(_connect_404(json_body=False), id="routing-404-html"),
        pytest.param(_response(404), id="404-empty-body"),
        pytest.param(_response(404, payload={"detail": "x", "code": "y"}), id="404-other-json"),
        pytest.param(_response(404, payload=["Not found."]), id="404-json-list"),
    ],
)
async def test_connect_unrecognized_answer_falls_back_to_the_listing(settings, response):
    settings.CONNECT_API_URL = "https://connect.example"
    result, requests = await _verify(
        _request("commcare_connect"),
        [response, _response(payload={"opportunities": [{"id": 7, "name": "Seven"}]})],
        settings=settings,
        external_ids={"7"},
    )

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.scoped is False
    assert [url for url, _kwargs in requests] == [
        "https://connect.example/export/opportunity/7/",
        "https://connect.example/export/opp_org_program_list/",
    ]


@pytest.mark.asyncio
async def test_connect_fallback_discards_partial_light_results(settings):
    settings.CONNECT_API_URL = "https://connect.example"
    result, requests = await _verify(
        _request("commcare_connect"),
        [
            _response(payload={"id": 3}),
            _response(payload={"id": 99}),
            _response(payload={"opportunities": [{"id": 3, "name": "Three"}]}),
        ],
        settings=settings,
        external_ids={"3", "7"},
    )

    # The listing is authoritative for the whole connection, so its answer alone
    # stands: 7 is omitted there, whatever the light check saw first.
    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.scoped is False
    assert result.external_ids == frozenset({"3"})
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        # 5s of the 20s kept back for the listing; each request is still capped at 10s.
        pytest.param(20.0, 10.0, id="per-request-cap"),
        # Concurrent, so every request may use all that is left, not a share of it.
        pytest.param(14.0, 9.0, id="remaining"),
        # Under 10s, half is kept back.
        pytest.param(6.0, 3.0, id="reserve-half"),
    ],
)
async def test_connect_light_check_budget(settings, deadline, expected):
    settings.CONNECT_API_URL = "https://connect.example"
    ids = ["1", "2", "3", "4"]
    _result, requests = await _verify(
        _request("commcare_connect"),
        [_response(payload={"id": int(i)}) for i in ids],
        settings=settings,
        deadline=deadline,
        external_ids=set(ids),
    )

    assert {kwargs["timeout"] for _url, kwargs in requests} == {expected}


class _GatedClient:
    """Answers each opportunity only once ``gate`` requests are in flight together."""

    def __init__(self, gate):
        self.gate = gate
        self.in_flight = 0
        self.peak = 0
        self.urls = []
        self.released = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.urls.append(url)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.in_flight >= self.gate:
            self.released.set()
        await self.released.wait()
        self.in_flight -= 1
        opp_id = int(url.rstrip("/").rsplit("/", 1)[1])
        return _response(payload={"id": opp_id}, url=url)


@pytest.mark.asyncio
async def test_connect_checks_a_seven_source_workspace_concurrently(settings):
    """A 7-opportunity workspace for a user with hundreds of opportunities was sent to
    the full export, which outlasts the interactive budget (KC - 12 opps, 2026-10-09)."""
    settings.CONNECT_API_URL = "https://connect.example"
    ids = {"523", "524", "874", "938", "1487", "1488", "2166"}
    client = _GatedClient(gate=len(ids))

    result = await asyncio.wait_for(
        verify_provider(
            _request("commcare_connect"),
            deadline=100.0,
            clock=lambda: 0.0,
            client_factory=lambda: client,
            settings=settings,
            limiter=asyncio.Semaphore(1),
            external_ids=frozenset(ids),
        ),
        timeout=5,
    )

    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.external_ids == frozenset(ids)
    assert result.scope == frozenset(ids)
    assert client.peak == len(ids)
    assert not any("opp_org_program_list" in url for url in client.urls)


@pytest.mark.asyncio
async def test_connect_light_check_bounds_its_concurrency(settings):
    settings.CONNECT_API_URL = "https://connect.example"
    ids = {str(i) for i in range(1, CONNECT_LIGHT_CONCURRENCY + 4)}
    client = _GatedClient(gate=CONNECT_LIGHT_CONCURRENCY)

    result = await asyncio.wait_for(
        verify_provider(
            _request("commcare_connect"),
            deadline=100.0,
            clock=lambda: 0.0,
            client_factory=lambda: client,
            settings=settings,
            limiter=asyncio.Semaphore(1),
            external_ids=frozenset(ids),
        ),
        timeout=5,
    )

    assert result.external_ids == frozenset(ids)
    assert client.peak == CONNECT_LIGHT_CONCURRENCY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (_response(401), VerificationOutcome.CREDENTIAL_REJECTED),
        (_response(429), VerificationOutcome.UNAVAILABLE),
        (_response(503), VerificationOutcome.UNAVAILABLE),
        (httpx.ConnectError("down"), VerificationOutcome.UNAVAILABLE),
    ],
)
async def test_connect_light_check_keeps_credential_and_transient_semantics(
    settings, response, outcome
):
    settings.CONNECT_API_URL = "https://connect.example"
    result, _ = await _verify(
        _request("commcare_connect"), [response], settings=settings, external_ids={"7"}
    )
    assert result.outcome == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "external_ids",
    [
        pytest.param(frozenset(), id="none-requested"),
        pytest.param(frozenset(str(i) for i in range(1, MAX_SUBSET + 2)), id="above-cap"),
        pytest.param(frozenset({"7", "abc"}), id="non-numeric"),
        pytest.param(frozenset({"007"}), id="zero-padded"),
    ],
)
async def test_connect_falls_back_to_the_full_listing(settings, external_ids):
    settings.CONNECT_API_URL = "https://connect.example"
    result, requests = await _verify(
        _request("commcare_connect"),
        [_response(payload={"opportunities": [{"id": 7, "name": "Seven"}]})],
        settings=settings,
        external_ids=external_ids,
    )
    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.scoped is False
    assert [url for url, _kwargs in requests] == [
        "https://connect.example/export/opp_org_program_list/"
    ]


@pytest.mark.asyncio
async def test_external_ids_do_not_change_other_providers(settings):
    settings.OCS_URL = "https://ocs.example"
    result, requests = await _verify(
        _request("ocs"),
        [_response(payload={"results": [{"id": "bot"}], "next": None})],
        settings=settings,
        external_ids={"bot"},
    )
    assert result.outcome == VerificationOutcome.COMPLETE
    assert result.scoped is False
    assert [url for url, _kwargs in requests] == ["https://ocs.example/api/experiments/"]


class _HangingAfterFirstClient:
    """Answers the first opportunity with ``first``; every other request hangs."""

    def __init__(self, first):
        self.first = first
        self.cancelled = 0
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return self.first
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


@pytest.mark.asyncio
async def test_connect_decisive_answer_does_not_wait_for_a_hung_sibling(settings):
    settings.CONNECT_API_URL = "https://connect.example"
    client = _HangingAfterFirstClient(_response(401))

    result = await asyncio.wait_for(
        verify_provider(
            _request("commcare_connect"),
            deadline=100.0,
            clock=lambda: 0.0,
            client_factory=lambda: client,
            settings=settings,
            limiter=asyncio.Semaphore(1),
            external_ids=frozenset({"1", "2", "3"}),
        ),
        timeout=5,
    )

    assert result.outcome == VerificationOutcome.CREDENTIAL_REJECTED
    # Nothing is left running against the client once the check has returned.
    assert client.cancelled == 2
