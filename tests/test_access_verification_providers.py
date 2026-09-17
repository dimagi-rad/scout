from __future__ import annotations

import asyncio
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
from apps.users.services.access_verification_providers import ProcessNetworkLimiter, verify_provider
from apps.users.services.access_verification_types import (
    CredentialObservation,
    CredentialRequestSnapshot,
    VerificationOutcome,
)


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


async def _verify(request, responses, *, settings, deadline=100.0, clock=lambda: 0.0):
    client = _Client(responses)
    result = await verify_provider(
        request,
        deadline=deadline,
        clock=clock,
        client_factory=lambda: client,
        settings=settings,
        limiter=asyncio.Semaphore(1),
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
        assert peak == 4

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
    assert peak == 4
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
