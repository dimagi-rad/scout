from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from threading import BoundedSemaphore
from typing import Any

import httpx
from django.conf import settings as django_settings

from apps.common.commcare_servers import COMMCARE_SERVERS
from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection
from apps.users.services.access_verification_types import (
    CredentialRequestSnapshot,
    ProviderVerificationResult,
)
from apps.users.services.oauth_scope import canonical_provider
from apps.users.services.token_refresh import is_transient_status
from mcp_server.loaders._urls import ProviderURLPolicy, UnsafeProviderURL

PROVIDER_BUDGET_SECONDS = 20.0
PER_REQUEST_TIMEOUT_SECONDS = 10.0
MAX_PAGES = 100
MAX_ROWS = 10_000
# Above this many requested opportunities, one full listing beats one request each.
CONNECT_LIGHT_CHECK_MAX_OPPORTUNITIES = 5


class ProcessNetworkLimiter:
    """Bound network work across threads and successive async_to_sync loops.

    Never block an event loop or delegate a blocking acquisition to an executor:
    a cancelled executor waiter could acquire a permit after its caller exits.
    Nonblocking acquisition has no suspension between acquiring and returning;
    callers own release after a successful await, as with asyncio.Semaphore.
    """

    def __init__(self, capacity: int):
        self._permits = BoundedSemaphore(capacity)

    async def acquire(self) -> bool:
        # asyncio.Event/Semaphore cannot coordinate waiters on different loops.
        while not self._permits.acquire(blocking=False):  # noqa: ASYNC110
            # Cancellable backoff keeps the caller's wait_for deadline effective.
            await asyncio.sleep(0.01)
        return True

    def release(self) -> None:
        self._permits.release()


NETWORK_LIMITER = ProcessNetworkLimiter(4)

_UNAVAILABLE = "verification_unavailable"
_INDETERMINATE = "verification_indeterminate"


def _default_client_factory():
    return httpx.AsyncClient(timeout=PER_REQUEST_TIMEOUT_SECONDS)


def _provider_request(snapshot, settings):
    provider = canonical_provider(snapshot.observation.provider)
    credential_type = snapshot.observation.credential_type
    credential = snapshot.credential
    if provider == "commcare":
        server = COMMCARE_SERVERS.get(snapshot.observation.scope_key)
        if server is None:
            return None
        url = server.user_domains_url
        if credential_type == TenantConnection.OAUTH:
            headers = {"Authorization": f"Bearer {credential}"}
        elif credential_type == TenantConnection.API_KEY:
            username, separator, api_key = credential.partition(":")
            if not separator or not username or not api_key:
                return None
            headers = {"Authorization": f"ApiKey {username}:{api_key}"}
        else:
            return None
        return provider, url, headers
    if provider == "ocs":
        url = f"{settings.OCS_URL.rstrip('/')}/api/experiments/"
        if credential_type == TenantConnection.OAUTH:
            if not snapshot.observation.scope_key:
                return None
            headers = {"Authorization": f"Bearer {credential}"}
        elif credential_type == TenantConnection.API_KEY:
            headers = {"X-api-key": credential}
        else:
            return None
        return provider, url, headers
    if provider == "commcare_connect" and credential_type == TenantConnection.OAUTH:
        url = f"{settings.CONNECT_API_URL.rstrip('/')}/export/opp_org_program_list/"
        return provider, url, {"Authorization": f"Bearer {credential}"}
    return None


def _connect_light_ids(snapshot, external_ids) -> tuple[str, ...] | None:
    """The opportunity ids to check one by one, or None to use the full listing.

    ``/export/opp_org_program_list/`` exports every opportunity with a per-row
    visit count and takes ~10s for users with many opportunities, which alone
    exhausts the interactive budget. ``/export/opportunity/<id>/`` answers the
    question for one opportunity cheaply.
    """
    if (
        canonical_provider(snapshot.observation.provider) != "commcare_connect"
        or snapshot.observation.credential_type != TenantConnection.OAUTH
        or not external_ids
        or len(external_ids) > CONNECT_LIGHT_CHECK_MAX_OPPORTUNITIES
    ):
        return None
    ids = tuple(sorted(external_ids))
    # Connect routes ``<int:opp_id>``; any other id 404s at routing, which must
    # never be read as a denial, and a zero-padded one comes back renumbered.
    if not all(
        external_id.isascii() and external_id.isdigit() and str(int(external_id)) == external_id
        for external_id in ids
    ):
        return None
    return ids


def _is_connect_no_access_404(response) -> bool:
    """DRF's NotFound body, as opposed to a routing 404 (an HTML page) or a proxy's.

    Connect answers an opportunity the user may not export via
    ``_get_opportunity_or_404`` with ``{"detail": "Not found."}``. ``detail`` is
    localized, so only the shape is checked.
    """
    if response.status_code != 404:
        return False
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and set(payload) == {"detail"}
        and isinstance(payload["detail"], str)
    )


def _is_requested_opportunity(response, external_id: str) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    raw_id = payload.get("id")
    return (
        not isinstance(raw_id, bool)
        and isinstance(raw_id, (int, str))
        and str(raw_id) == external_id
    )


async def _verify_connect_opportunities(client, policy, listing_url, headers, ids, deadline, clock):
    """Check each opportunity; None means fall back to the full listing.

    A no-access 404 drops that opportunity, which publication then archives as an
    omission, exactly as the listing would have; the rest are still checked.
    """
    confirmed = []
    for index, external_id in enumerate(ids):
        remaining = deadline - clock()
        if remaining <= 0:
            return ProviderVerificationResult.unavailable(_UNAVAILABLE)
        try:
            url = policy.resolve(f"../opportunity/{external_id}/", relative_to=listing_url)
        except UnsafeProviderURL:
            return ProviderVerificationResult.indeterminate(_INDETERMINATE)
        # Share what is left, so one slow opportunity cannot starve the rest.
        request_timeout = min(PER_REQUEST_TIMEOUT_SECONDS, remaining / (len(ids) - index))
        try:
            response = await asyncio.wait_for(
                client.get(
                    url,
                    headers=headers,
                    follow_redirects=False,
                    timeout=request_timeout,
                ),
                timeout=request_timeout,
            )
        except (httpx.RequestError, TimeoutError):
            return ProviderVerificationResult.unavailable(_UNAVAILABLE)
        if clock() >= deadline:
            return ProviderVerificationResult.unavailable(_UNAVAILABLE)
        if _is_connect_no_access_404(response):
            continue
        if response.status_code == 404:
            # Not DRF's answer, so likely the route itself is gone; the listing
            # can still decide, and must never read this as an omission.
            return None
        status_result = _status_result(response.status_code)
        if status_result is not None:
            return status_result
        if not _is_requested_opportunity(response, external_id):
            # A changed response shape must cost a slow check, not every check.
            return None
        confirmed.append(external_id)
    return ProviderVerificationResult.complete(confirmed, scoped=True)


def _status_result(status_code: int):
    if status_code == 401:
        return ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED)
    if is_transient_status(status_code):
        return ProviderVerificationResult.unavailable(_UNAVAILABLE)
    if status_code < 200 or status_code >= 300:
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)
    return None


def _row_identity(row: Any, *, id_key: str, name_key: str) -> tuple[str, str]:
    if not isinstance(row, dict):
        raise TypeError("provider row must be an object")
    raw_id = row.get(id_key)
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        raise TypeError("provider row has invalid id")
    external_id = str(raw_id).strip()
    if not external_id:
        raise ValueError("provider row has empty id")
    raw_name = row.get(name_key)
    if raw_name is not None and not isinstance(raw_name, str):
        raise ValueError("provider row has invalid name")
    return external_id, (raw_name or external_id)


def _add_rows(
    rows: Any,
    seen: dict[str, str],
    *,
    id_key: str,
    name_key: str,
) -> None:
    if not isinstance(rows, list):
        raise TypeError("provider collection must be a list")
    for row in rows:
        external_id, name = _row_identity(row, id_key=id_key, name_key=name_key)
        previous_name = seen.setdefault(external_id, name)
        if previous_name != name:
            raise ValueError("provider returned conflicting duplicate ids")


def _parse_page(provider: str, payload: Any, seen: dict[str, str]):
    if not isinstance(payload, dict):
        raise TypeError("provider response must be an object")
    if provider == "commcare":
        if "objects" not in payload or not isinstance(payload.get("meta"), dict):
            raise ValueError("CommCare response is incomplete")
        meta = payload["meta"]
        if "next" not in meta:
            raise ValueError("CommCare pagination metadata is incomplete")
        _add_rows(payload["objects"], seen, id_key="domain_name", name_key="project_name")
        return meta["next"], len(payload["objects"])
    if provider == "ocs":
        if "results" not in payload or "next" not in payload:
            raise ValueError("OCS response is incomplete")
        _add_rows(payload["results"], seen, id_key="id", name_key="name")
        return payload["next"], len(payload["results"])
    pagination_keys = {
        "next",
        "previous",
        "pagination",
        "meta",
        "has_more",
        "next_page",
        "count",
        "page",
        "page_size",
        "limit",
        "offset",
    }
    if "opportunities" not in payload or pagination_keys.intersection(payload):
        raise ValueError("Connect response is incomplete or paginated")
    _add_rows(payload["opportunities"], seen, id_key="id", name_key="name")
    return None, len(payload["opportunities"])


async def verify_provider(
    snapshot: CredentialRequestSnapshot,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    client_factory: Callable[[], Any] = _default_client_factory,
    settings=django_settings,
    limiter: ProcessNetworkLimiter | asyncio.Semaphore = NETWORK_LIMITER,
    external_ids: frozenset[str] = frozenset(),
) -> ProviderVerificationResult:
    """Return a complete, bounded provider listing without touching the database.

    ``external_ids`` are the tenants the caller needs. Connect checks only those
    when there are few, and its result is then authoritative only for them.
    """
    deadline = min(
        deadline if deadline is not None else float("inf"),
        clock() + PROVIDER_BUDGET_SECONDS,
    )
    request = _provider_request(snapshot, settings)
    if request is None:
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)
    provider, initial_url, headers = request
    light_ids = _connect_light_ids(snapshot, external_ids)
    try:
        policy = ProviderURLPolicy(initial_url)
        url = policy.resolve(initial_url)
    except UnsafeProviderURL:
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)

    remaining = deadline - clock()
    if remaining <= 0:
        return ProviderVerificationResult.unavailable(_UNAVAILABLE)
    acquired = False
    try:
        # Take the permit through an explicit handle so the ownership transfer is
        # observable. stdlib wait_for does rescue a permit taken just as the wait
        # is cut short -- 3.11 returns the inner future's result, 3.12+ runs the
        # coroutine inline with no window -- but that is a version-specific
        # implementation detail, and a permit lost here would starve this
        # process-wide limiter for the life of the process.
        waiter = asyncio.ensure_future(limiter.acquire())
        try:
            await asyncio.wait_for(waiter, timeout=remaining)
        except BaseException:
            if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                limiter.release()
            raise
        acquired = True
        seen_urls: set[str] = set()
        seen_rows: dict[str, str] = {}
        total_rows = 0
        async with client_factory() as client:
            if light_ids is not None:
                light_result = await _verify_connect_opportunities(
                    client, policy, url, headers, light_ids, deadline, clock
                )
                if light_result is not None:
                    return light_result
            for _page_number in range(MAX_PAGES):
                if url in seen_urls:
                    return ProviderVerificationResult.indeterminate(_INDETERMINATE)
                seen_urls.add(url)
                remaining = deadline - clock()
                if remaining <= 0:
                    return ProviderVerificationResult.unavailable(_UNAVAILABLE)
                try:
                    request_timeout = min(PER_REQUEST_TIMEOUT_SECONDS, remaining)
                    response = await asyncio.wait_for(
                        client.get(
                            url,
                            headers=headers,
                            follow_redirects=False,
                            timeout=request_timeout,
                        ),
                        timeout=request_timeout,
                    )
                except (httpx.RequestError, TimeoutError):
                    return ProviderVerificationResult.unavailable(_UNAVAILABLE)
                if clock() >= deadline:
                    return ProviderVerificationResult.unavailable(_UNAVAILABLE)
                status_result = _status_result(response.status_code)
                if status_result is not None:
                    return status_result
                try:
                    next_reference, page_rows = _parse_page(provider, response.json(), seen_rows)
                except (ValueError, TypeError):
                    return ProviderVerificationResult.indeterminate(_INDETERMINATE)
                total_rows += page_rows
                if total_rows > MAX_ROWS:
                    return ProviderVerificationResult.indeterminate(_INDETERMINATE)
                if next_reference is None:
                    return ProviderVerificationResult.complete(seen_rows)
                if not isinstance(next_reference, str) or not next_reference:
                    return ProviderVerificationResult.indeterminate(_INDETERMINATE)
                try:
                    url = policy.resolve(next_reference, relative_to=url)
                except UnsafeProviderURL:
                    return ProviderVerificationResult.indeterminate(_INDETERMINATE)
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)
    except TimeoutError:
        return ProviderVerificationResult.unavailable(_UNAVAILABLE)
    finally:
        if acquired:
            limiter.release()
