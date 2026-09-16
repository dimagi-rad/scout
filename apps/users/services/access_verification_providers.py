from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from threading import BoundedSemaphore
from typing import Any

import httpx
from django.conf import settings as django_settings

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection
from apps.users.services.access_verification_types import (
    CredentialRequestSnapshot,
    ProviderVerificationResult,
)
from apps.users.services.oauth_scope import canonical_provider
from mcp_server.loaders._urls import ProviderURLPolicy, UnsafeProviderURL

PROVIDER_BUDGET_SECONDS = 20.0
PER_REQUEST_TIMEOUT_SECONDS = 10.0
MAX_PAGES = 100
MAX_ROWS = 10_000


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
        url = "https://www.commcarehq.org/api/user_domains/v1/"
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


def _status_result(status_code: int):
    if status_code == 401:
        return ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED)
    if status_code in (408, 429) or status_code >= 500:
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
) -> ProviderVerificationResult:
    """Return a complete, bounded provider listing without touching the database."""
    deadline = min(
        deadline if deadline is not None else float("inf"),
        clock() + PROVIDER_BUDGET_SECONDS,
    )
    request = _provider_request(snapshot, settings)
    if request is None:
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)
    provider, initial_url, headers = request
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
        await asyncio.wait_for(limiter.acquire(), timeout=remaining)
        acquired = True
        seen_urls: set[str] = set()
        seen_rows: dict[str, str] = {}
        total_rows = 0
        async with client_factory() as client:
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
