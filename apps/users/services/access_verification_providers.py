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
from apps.users.services.tenant_listing import connect as connect_listing
from apps.users.services.tenant_listing import ocs as ocs_listing
from apps.users.services.tenant_listing.paginator import paginate
from apps.users.services.tenant_listing.types import (
    ListingDeadlineExceeded,
    RequestTimedOut,
    TenantListError,
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
    observation = snapshot.observation
    if provider == "commcare":
        request = commcare_listing.list_request(
            observation.scope_key, observation.credential_type, snapshot.credential
        )
        decode_page = commcare_listing.decode_page
    elif provider == "ocs":
        # An OAuth connection's proof is per team; without one there is nothing to verify.
        if observation.credential_type == TenantConnection.OAUTH and not observation.scope_key:
            return None
        request = ocs_listing.list_request(
            settings.OCS_URL, observation.credential_type, snapshot.credential
        )
        decode_page = ocs_listing.decode_page
    elif provider == "commcare_connect":
        request = connect_listing.list_request(
            settings.CONNECT_API_URL, observation.credential_type, snapshot.credential
        )
        decode_page = connect_listing.decode_page
    else:
        return None
    return None if request is None else (provider, request, decode_page)


def _connect_light_ids(snapshot, external_ids) -> tuple[str, ...] | None:
    """The opportunities to check one by one, or None to use the full listing."""
    if (
        canonical_provider(snapshot.observation.provider) != "commcare_connect"
        or snapshot.observation.credential_type != TenantConnection.OAUTH
    ):
        return None
    return connect_listing.subset_ids(external_ids)


async def _verify_connect_opportunities(
    client, listing, ids, deadline, clock, *, connection_id, log, unavailable
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
            request = connect_listing.verify_subset_request(listing, external_id)
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
                    request.url,
                    headers=dict(request.headers),
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
        if connect_listing.is_no_access_answer(response):
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
        if not connect_listing.confirms_opportunity(response, external_id):
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
    provider = canonical_provider(snapshot.observation.provider)

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

    # Logged because an indeterminate answer denies the workspace and the user cannot
    # retry past it; #880 left no trace while these returns were silent.
    def indeterminate(cause, *, page=0, status=None):
        log(cause, page=page, status=status, outcome="indeterminate")
        return ProviderVerificationResult.indeterminate(_INDETERMINATE)

    request = _provider_request(snapshot, settings)
    if request is None:
        return indeterminate("no_request")
    _, list_request, decode_page = request
    light_ids = _connect_light_ids(snapshot, external_ids)

    try:
        ProviderURLPolicy(list_request.url)
    except UnsafeProviderURL:
        return indeterminate("unsafe_url")

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
                    list_request,
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
                            return indeterminate("next_undeclared", page=page)
                        total_rows += len(listed.tenants)
                        if total_rows > MAX_ROWS:
                            return indeterminate("row_cap", page=page)
                        for tenant in listed.tenants:
                            name = seen_rows.setdefault(tenant.external_id, tenant.canonical_name)
                            if name != tenant.canonical_name:
                                # One id under two names: the listing cannot be trusted.
                                return indeterminate("conflicting_names", page=page)
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
                if status_result.outcome in (
                    VerificationOutcome.UNAVAILABLE,
                    VerificationOutcome.INDETERMINATE,
                ):
                    log(
                        "http_status",
                        page=error.page,
                        status=error.status_code,
                        outcome=status_result.outcome.value,
                    )
                return status_result
            except TenantListError as error:
                # Off-origin next, a cycle, the page cap or a malformed page.
                return indeterminate(f"list_error:{type(error).__name__}", page=page)
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
