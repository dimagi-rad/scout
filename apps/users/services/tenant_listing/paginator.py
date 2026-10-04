"""Follow a provider's tenant list to its end, or fail saying why.

Each caller decides what a failure means (discovery skips the refresh, onboarding
rejects the form, verification reports unavailable or indeterminate), so this
module only names failures; it never maps them, and never writes ORM rows.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from typing import Any

import httpx

from apps.users.services.tenant_listing.types import (
    ListingDeadlineExceeded,
    MalformedTenantList,
    PageLimitReached,
    PaginationCycle,
    ProviderRequest,
    RequestTimedOut,
    TenantDescriptor,
    TenantListPage,
    UnsafeListingOrigin,
    UnsafeNextURL,
    UpstreamStatus,
    UpstreamUnreachable,
)
from mcp_server.loaders._urls import ProviderURLPolicy, UnsafeProviderURL


async def paginate(
    client: httpx.AsyncClient,
    request: ProviderRequest,
    decode_page: Callable[[Any], TenantListPage],
    *,
    deadline: float,
    max_pages: int,
    request_timeout: float,
    clock: Callable[[], float] | None = None,
) -> AsyncIterator[TenantListPage]:
    """Yield each page, following ``next_url`` only within the request's origin.

    Redirects are never followed, every request is cut off at the remaining budget,
    and a response that lands after the deadline is not trusted.
    """
    clock = clock or time.monotonic
    try:
        policy = ProviderURLPolicy(request.url)
    except UnsafeProviderURL as error:
        raise UnsafeListingOrigin(str(error)) from None
    url = policy.base_url
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        if url in seen:
            raise PaginationCycle(page=page)
        seen.add(url)
        remaining = deadline - clock()
        if remaining <= 0:
            raise ListingDeadlineExceeded(page=page)
        timeout = min(request_timeout, remaining)
        try:
            response = await asyncio.wait_for(
                client.get(
                    url, headers=dict(request.headers), follow_redirects=False, timeout=timeout
                ),
                timeout=timeout,
            )
        except TimeoutError:
            raise RequestTimedOut(page=page) from None
        except httpx.RequestError as error:
            raise UpstreamUnreachable(error, page=page) from None
        if clock() >= deadline:
            raise ListingDeadlineExceeded(page=page, status_code=response.status_code)
        if not response.is_success:
            raise UpstreamStatus(response, page=page)
        try:
            payload = response.json()
        except ValueError:
            raise MalformedTenantList("not JSON", page=page) from None
        try:
            decoded = decode_page(payload)
        except MalformedTenantList as error:
            error.page = page
            raise
        yield decoded
        if decoded.next_url is None:
            return
        try:
            url = policy.resolve(decoded.next_url, relative_to=url)
        except UnsafeProviderURL:
            raise UnsafeNextURL(page=page) from None
    raise PageLimitReached(page=max_pages)


async def list_tenants(
    client: httpx.AsyncClient,
    request: ProviderRequest,
    decode_page: Callable[[Any], TenantListPage],
    *,
    budget_seconds: float,
    max_pages: int,
    request_timeout: float,
    clock: Callable[[], float] | None = None,
) -> list[TenantDescriptor]:
    """Every tenant on every page, in order; duplicates are the caller's to judge."""
    clock = clock or time.monotonic
    tenants: list[TenantDescriptor] = []
    pages = paginate(
        client,
        request,
        decode_page,
        deadline=clock() + budget_seconds,
        max_pages=max_pages,
        request_timeout=request_timeout,
        clock=clock,
    )
    async with aclosing(pages):
        async for page in pages:
            tenants.extend(page.tenants)
    return tenants
