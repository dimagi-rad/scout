from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import aclosing
from threading import BoundedSemaphore
from typing import Any

import httpx
from django.conf import settings as django_settings

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection
from apps.users.services.access_verification_types import (
    CredentialRequestSnapshot,
    ProviderVerificationResult,
    VerificationOutcome,
)
from apps.users.services.oauth_scope import canonical_provider
from apps.users.services.tenant_listing import commcare as commcare_listing
from apps.users.services.tenant_listing.paginator import paginate
from apps.users.services.tenant_listing.rows import descriptor
from apps.users.services.tenant_listing.types import (
    ListingDeadlineExceeded,
    MalformedTenantList,
    ProviderRequest,
    RequestTimedOut,
    TenantListError,
    TenantListPage,
    UpstreamStatus,
    UpstreamUnreachable,
)
from apps.users.services.token_refresh import is_transient_status
from mcp_server.loaders._urls import ProviderURLPolicy, UnsafeProviderURL

logger = logging.getLogger(__name__)

PROVIDER_BUDGET_SECONDS = 20.0
PER_REQUEST_TIMEOUT_SECONDS = 10.0
MAX_PAGES = 100
MAX_ROWS = 10_000
# Above this many requested opportunities, one full listing beats one request each.
CONNECT_LIGHT_CHECK_MAX_OPPORTUNITIES = 5
CONNECT_LIGHT_REQUEST_FLOOR_SECONDS = 4.0
CONNECT_LISTING_RESERVE_SECONDS = 5.0


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

logger = logging.getLogger(__name__)

_UNAVAILABLE = "verification_unavailable"
_INDETERMINATE = "verification_indeterminate"


def _log_unconfirmed(
    snapshot, provider, cause, *, started, deadline, clock, page=0, status=None, outcome
):
    # WARNING, not ERROR: Sentry's logging integration only turns ERROR records into
    # events, so this is a breadcrumb plus a container log line, not a page. Never
    # add the URL or headers here -- they carry the credential. budget_ms is what the
    # caller's deadline left when this call started, separating a slow provider from
    # a budget already spent upstream of it.
    observation = snapshot.observation
    logger.warning(
        "Upstream access verification %s: provider=%s cause=%s status=%s "
        "elapsed_ms=%d budget_ms=%s page=%d connection_id=%s user_id=%s scope=%s",
        outcome,
        provider,
        cause,
        status if status is not None else "-",
        max(0, round((clock() - started) * 1000)),
        max(0, round((deadline - started) * 1000)),
        page,
        observation.connection_id,
        observation.user_id,
        observation.scope_key or "-",
    )


def _default_client_factory():
    return httpx.AsyncClient(timeout=PER_REQUEST_TIMEOUT_SECONDS)


def _provider_request(snapshot, settings):
    provider = canonical_provider(snapshot.observation.provider)
    credential_type = snapshot.observation.credential_type
    credential = snapshot.credential
    if provider == "commcare":
        request = commcare_listing.list_request(
            snapshot.observation.scope_key, credential_type, credential
        )
        return None if request is None else (provider, request, commcare_listing.decode_page)
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
        return provider, ProviderRequest(url, headers), _decode_ocs_page
    if provider == "commcare_connect" and credential_type == TenantConnection.OAUTH:
        url = f"{settings.CONNECT_API_URL.rstrip('/')}/export/opp_org_program_list/"
        request = ProviderRequest(url, {"Authorization": f"Bearer {credential}"})
        return provider, request, _decode_connect_page
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


async def _verify_connect_opportunities(
    client, policy, listing_url, headers, ids, deadline, clock, *, connection_id, log, unavailable
):
    """Check each opportunity; None means fall back to the full listing.

    A no-access 404 is an omission, which publication archives as the listing would
    have. Once one is seen, a later failure ends the check with what was decided so
    far rather than discarding that revocation.
    """
    # Keep part of the budget back, or the listing fallback could never finish.
    light_deadline = deadline - min(CONNECT_LISTING_RESERVE_SECONDS, (deadline - clock()) / 2)
    confirmed = []
    omitted = False

    def settle(index, failure, *, cause, status=None):
        """``failure`` is a thunk, so a settled attempt does not log as unavailable."""
        if omitted:
            log(f"settled_after_omission:{cause}", status=status, outcome="partial")
            return ProviderVerificationResult.complete(confirmed, scope=ids[:index])
        return failure()

    for index, external_id in enumerate(ids):
        remaining = light_deadline - clock()
        if remaining <= 0:
            return settle(
                index,
                lambda: unavailable("light_deadline_before_request"),
                cause="light_deadline_before_request",
            )
        try:
            url = policy.resolve(f"../opportunity/{external_id}/", relative_to=listing_url)
        except UnsafeProviderURL:
            return settle(
                index,
                lambda: ProviderVerificationResult.indeterminate(_INDETERMINATE),
                cause="unsafe_url",
            )
        # A share of what is left, so one slow opportunity cannot starve the rest,
        # but never so small that an ordinary slow answer is cut off. On the 10s
        # interactive budget the floor wins, so allocation is effectively greedy.
        request_timeout = min(
            PER_REQUEST_TIMEOUT_SECONDS,
            remaining,
            max(remaining / (len(ids) - index), CONNECT_LIGHT_REQUEST_FLOOR_SECONDS),
        )
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
        except TimeoutError:
            return settle(
                index, lambda: unavailable("light_request_timeout"), cause="light_request_timeout"
            )
        except httpx.RequestError as exc:
            # The class name only: str(exc) can carry the request URL.
            cause = f"light_request_error:{type(exc).__name__}"
            return settle(index, lambda cause=cause: unavailable(cause), cause=cause)
        if clock() >= deadline:
            status = response.status_code
            return settle(
                index,
                lambda status=status: unavailable("light_deadline_after_response", status=status),
                cause="light_deadline_after_response",
                status=status,
            )
        if _is_connect_no_access_404(response):
            omitted = True
            continue
        if response.status_code == 404:
            # Not DRF's answer, so likely the route itself is gone; the listing
            # can still decide, and must never read this as an omission.
            return settle(index, lambda: None, cause="route_404", status=404)
        status_result = _status_result(response.status_code)
        if response.status_code == 401:
            logger.info(
                "Provider commcare_connect answered verification for connection %s "
                "with HTTP 401 (%s)",
                connection_id,
                "invalid_token" if _names_invalid_token(response) else "no token error",
            )
            # Credential-level, so it outranks a per-opportunity omission.
            return status_result
        if status_result is not None:

            def failed(result=status_result, status=response.status_code):
                if result.outcome == VerificationOutcome.UNAVAILABLE:
                    log("light_http_status", status=status)
                return result

            return settle(index, failed, cause="light_http_status", status=response.status_code)
        if not _is_requested_opportunity(response, external_id):
            # A changed response shape must cost a slow check, not every check.
            return settle(
                index, lambda: None, cause="unrecognized_answer", status=response.status_code
            )
        confirmed.append(external_id)
    return ProviderVerificationResult.complete(confirmed, scope=ids)


def _names_invalid_token(response) -> bool:
    """Whether a 401 blames the token itself (RFC 6750 3.1), not the caller's access.

    A predicate so only a constant reaches the log: OCS answers 401 both for a dead
    token and for a valid one whose user has left the team. Diagnostic only for now;
    nothing decides on it until production shows which one OCS sends for which case.
    """
    return 'error="invalid_token"' in response.headers.get("www-authenticate", "")


def _status_result(status_code: int):
    if status_code == 401:
        return ProviderVerificationResult.credential_rejected(ErrorCode.AUTH_TOKEN_EXPIRED)
    if is_transient_status(status_code):
        return ProviderVerificationResult.unavailable(_UNAVAILABLE)
    if status_code < 200 or status_code >= 300:
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)
    return None


def _decode_ocs_page(payload: Any) -> TenantListPage:
    if not isinstance(payload, dict) or "results" not in payload or "next" not in payload:
        raise MalformedTenantList("OCS response is incomplete")
    rows, next_url = payload["results"], payload["next"]
    if not isinstance(rows, list):
        raise MalformedTenantList("OCS results are not a list")
    if next_url is not None and (not isinstance(next_url, str) or not next_url):
        raise MalformedTenantList("OCS next link is not a URL")
    return TenantListPage(
        tuple(descriptor(row, id_key="id", name_key="name") for row in rows), next_url
    )


_CONNECT_PAGINATION_KEYS = frozenset(
    {
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
)


def _decode_connect_page(payload: Any) -> TenantListPage:
    if (
        not isinstance(payload, dict)
        or "opportunities" not in payload
        or _CONNECT_PAGINATION_KEYS.intersection(payload)
    ):
        raise MalformedTenantList("Connect response is incomplete or paginated")
    rows = payload["opportunities"]
    if not isinstance(rows, list):
        raise MalformedTenantList("Connect opportunities are not a list")
    return TenantListPage(
        tuple(descriptor(row, id_key="id", name_key="name") for row in rows), None
    )


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
    started = clock()
    deadline = min(
        deadline if deadline is not None else float("inf"),
        clock() + PROVIDER_BUDGET_SECONDS,
    )
    request = _provider_request(snapshot, settings)
    if request is None:
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)
    provider, list_request, decode_page = request
    initial_url, headers = list_request.url, dict(list_request.headers)
    light_ids = _connect_light_ids(snapshot, external_ids)

    def log(cause, *, page=0, status=None, outcome="unavailable"):
        _log_unconfirmed(
            snapshot,
            provider,
            cause,
            started=started,
            deadline=deadline,
            clock=clock,
            page=page,
            status=status,
            outcome=outcome,
        )

    def unavailable(cause, *, page=0, status=None):
        log(cause, page=page, status=status)
        return ProviderVerificationResult.unavailable(_UNAVAILABLE)

    try:
        policy = ProviderURLPolicy(initial_url)
        url = policy.resolve(initial_url)
    except UnsafeProviderURL:
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)

    remaining = deadline - clock()
    if remaining <= 0:
        return unavailable("deadline_before_start")
    acquired = False
    page = 0
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
        async with client_factory() as client:
            if light_ids is not None:
                light_result = await _verify_connect_opportunities(
                    client,
                    policy,
                    url,
                    headers,
                    light_ids,
                    deadline,
                    clock,
                    connection_id=snapshot.observation.connection_id,
                    log=log,
                    unavailable=unavailable,
                )
                if light_result is not None:
                    return light_result
            page = 1
            seen_rows: dict[str, str] = {}
            total_rows = 0
            pages = paginate(
                client,
                list_request,
                decode_page,
                deadline=deadline,
                max_pages=MAX_PAGES,
                request_timeout=PER_REQUEST_TIMEOUT_SECONDS,
                clock=clock,
            )
            try:
                async with aclosing(pages):
                    async for listed in pages:
                        if not listed.next_declared:
                            return ProviderVerificationResult.indeterminate(_INDETERMINATE)
                        total_rows += len(listed.tenants)
                        if total_rows > MAX_ROWS:
                            return ProviderVerificationResult.indeterminate(_INDETERMINATE)
                        for tenant in listed.tenants:
                            name = seen_rows.setdefault(tenant.external_id, tenant.canonical_name)
                            if name != tenant.canonical_name:
                                # One id under two names: the listing cannot be trusted.
                                return ProviderVerificationResult.indeterminate(_INDETERMINATE)
                        page += 1
            except ListingDeadlineExceeded as error:
                if error.status_code is None:
                    return unavailable("deadline_before_request", page=error.page)
                return unavailable(
                    "deadline_after_response", page=error.page, status=error.status_code
                )
            except RequestTimedOut as error:
                return unavailable("request_timeout", page=error.page)
            except UpstreamUnreachable as error:
                # The class name only: str(exc) can carry the request URL.
                return unavailable(f"request_error:{type(error.cause).__name__}", page=error.page)
            except UpstreamStatus as error:
                status_result = _status_result(error.status_code)
                if error.status_code == 401:
                    logger.info(
                        "Provider %s answered verification for connection %s with HTTP 401 (%s)",
                        provider,
                        snapshot.observation.connection_id,
                        "invalid_token"
                        if _names_invalid_token(error.response)
                        else "no token error",
                    )
                if status_result.outcome == VerificationOutcome.UNAVAILABLE:
                    log("http_status", page=error.page, status=error.status_code)
                return status_result
            except TenantListError:
                # Off-origin next, a cycle, the page cap or a malformed page.
                return ProviderVerificationResult.indeterminate(_INDETERMINATE)
            return ProviderVerificationResult.complete(seen_rows)
    except TimeoutError:
        # Before the permit is held this is the shared limiter wait; after, a stray
        # timeout from the client's context manager.
        return unavailable("timeout" if acquired else "limiter_wait_timeout", page=page)
    except asyncio.CancelledError:
        # The caller's own wait_for shares this deadline and can win the race, which
        # it reports as unavailable; log it so that outcome is not silent either. A
        # client disconnect or shutdown lands here too, hence "cancelled".
        log(
            "cancelled" if acquired else "cancelled_in_limiter_wait",
            page=page,
            outcome="cancelled",
        )
        raise
    finally:
        if acquired:
            limiter.release()
