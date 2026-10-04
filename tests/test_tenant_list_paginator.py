"""The shared tenant-list paginator; per-path behaviour is in test_tenant_list_conformance."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import httpx
import pytest

from apps.common.commcare_servers import COMMCARE_SERVERS
from apps.users.services.tenant_listing import commcare as commcare_listing
from apps.users.services.tenant_listing.paginator import list_tenants, paginate
from apps.users.services.tenant_listing.types import (
    MalformedTenantList,
    ProviderRequest,
    RequestTimedOut,
    TenantDescriptor,
    UpstreamStatus,
)
from apps.users.services.tenant_resolution import TenantResolutionError, _fetch_all_domains

LISTING = "https://provider.example/api/list/"
REQUEST = ProviderRequest(LISTING, {"Authorization": "Bearer secret"})


class _Client:
    def __init__(self, *responses, delay=0.0):
        self.responses = list(responses)
        self.delay = delay
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.responses.pop(0)


def _page(rows, next_url=None, status=200):
    return httpx.Response(
        status,
        json={"objects": rows, "meta": {"next": next_url}},
        request=httpx.Request("GET", LISTING),
    )


async def _collect(client, *, clock, budget=60.0, request_timeout=30.0):
    return await list_tenants(
        client,
        REQUEST,
        commcare_listing.decode_page,
        budget_seconds=budget,
        max_pages=10,
        request_timeout=request_timeout,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_each_request_is_cut_off_at_the_remaining_budget():
    now = [0.0]
    client = _Client(_page([{"domain_name": "a"}], "?offset=1"), _page([{"domain_name": "b"}]))
    get = client.get

    async def slow_first_page(url, **kwargs):
        response = await get(url, **kwargs)
        if len(client.calls) == 1:
            now[0] += 55.0
        return response

    client.get = slow_first_page
    tenants = await _collect(client, clock=lambda: now[0])

    assert [tenant.external_id for tenant in tenants] == ["a", "b"]
    # 55s spent on page 1 leaves 5s of the 60s budget, so page 2 gets 5s, not 30s.
    assert [kwargs["timeout"] for _, kwargs in client.calls] == [30.0, 5.0]


@pytest.mark.asyncio
async def test_a_request_that_hangs_past_the_budget_is_abandoned():
    client = _Client(_page([]), delay=5.0)
    started = time.monotonic()

    with pytest.raises(RequestTimedOut):
        await _collect(client, clock=time.monotonic, budget=0.05)

    assert time.monotonic() - started < 2
    assert client.calls[0][1]["timeout"] == pytest.approx(0.05, abs=0.05)


@pytest.mark.asyncio
async def test_redirects_are_never_followed_with_the_credential():
    client = _Client(_page([], status=302))

    with pytest.raises(UpstreamStatus) as raised:
        await _collect(client, clock=time.monotonic)

    assert raised.value.status_code == 302
    assert client.calls[0][1]["follow_redirects"] is False


@pytest.mark.asyncio
async def test_pages_are_yielded_as_they_arrive():
    client = _Client(
        _page([{"domain_name": "a", "project_name": "A"}], "?offset=1"),
        _page([{"domain_name": "b"}]),
    )
    pages = paginate(
        client,
        REQUEST,
        commcare_listing.decode_page,
        deadline=time.monotonic() + 60,
        max_pages=10,
        request_timeout=30,
    )

    seen = [page.tenants async for page in pages]

    assert seen == [(TenantDescriptor("a", "A"),), (TenantDescriptor("b", "b"),)]
    assert client.calls[1][0] == f"{LISTING}?offset=1"


@pytest.mark.parametrize(
    "payload",
    [
        {"objects": [], "meta": {"next": ""}},
        {"objects": [{"domain_name": True}], "meta": {"next": None}},
        {"objects": [{"domain_name": "a", "project_name": 7}], "meta": {"next": None}},
        {"objects": [], "meta": []},
    ],
)
def test_commcare_decoder_rejects_what_it_cannot_read(payload):
    with pytest.raises(MalformedTenantList):
        commcare_listing.decode_page(payload)


def test_commcare_decoder_reports_an_undeclared_next():
    page = commcare_listing.decode_page({"objects": [], "meta": {}})

    assert (page.next_url, page.next_declared) == (None, False)


def test_commcare_request_needs_a_known_server_and_a_whole_key():
    assert commcare_listing.list_request("mars", "oauth", "tok") is None
    assert commcare_listing.list_request("", "api_key", "no-separator") is None
    request = commcare_listing.list_request("eu", "api_key", "user@example.org:key")
    assert request.url == COMMCARE_SERVERS["eu"].user_domains_url
    assert request.headers == {"Authorization": "ApiKey user@example.org:key"}


@pytest.mark.asyncio
async def test_discovery_abandons_a_request_that_would_outlive_its_budget(httpx_mock):
    """R23 follow-up: the budget used to be checked only between pages."""

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"objects": [], "meta": {"next": None}})

    httpx_mock.add_callback(hang, is_optional=True)
    started = time.monotonic()

    with patch("apps.users.services.tenant_resolution._LISTING_BUDGET_SECONDS", 0.05):
        with pytest.raises(TenantResolutionError, match="did not finish"):
            await _fetch_all_domains("tok", COMMCARE_SERVERS[""])

    assert time.monotonic() - started < 2
