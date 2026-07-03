"""Shared HTTP hardening for the API loaders.

Centralises the bounded ``urllib3`` retry policy so CommCare, Connect, and OCS
loaders share one transport that survives transient upstream failures and
throttles without pinning the sole materialization worker thread (arch #252).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import requests
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# urllib3 honours a server ``Retry-After`` header verbatim when
# ``respect_retry_after_header=True`` — with NO upper bound (``backoff_max``
# caps only the exponential path). Loaders run on the single materialization
# worker thread, so an upstream throttle advertising a large ``Retry-After``
# would park that sole thread for the full value, up to ``total`` times per
# request, uncancellable (arch #252, finding 14#6). Clamp the honoured value so
# one throttle response costs at most this many seconds of sleep.
MAX_RETRY_AFTER_SECONDS = 30

RETRY_TOTAL = 3
RETRY_STATUS_FORCELIST = (500, 502, 503, 504, 408, 429)
RETRY_BACKOFF_FACTOR = 2.0


class BoundedRetry(Retry):
    """``urllib3.Retry`` that caps a server-supplied ``Retry-After``.

    Everything else is stock urllib3 behaviour; only the honoured
    ``Retry-After`` is clamped to ``MAX_RETRY_AFTER_SECONDS``.
    """

    def get_retry_after(self, response):
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return None
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)


def build_retry() -> Retry:
    """Return the shared bounded retry policy for loader sessions.

    ``backoff_factor=2.0`` yields 0s/2s/4s waits between the 4 total attempts
    on the exponential path; a server ``Retry-After`` is honoured but capped at
    ``MAX_RETRY_AFTER_SECONDS``. ``raise_on_status=False`` lets callers inspect
    the final response (status, headers) and raise a typed export error rather
    than propagating a raw ``requests.HTTPError``.
    """
    return BoundedRetry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=list(RETRY_STATUS_FORCELIST),
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )


# Returns a fresh OAuth access token (or None when no refresh is possible).
TokenRefresher = Callable[[], "str | None"]


def get_with_auth_refresh(
    session: requests.Session,
    url: str,
    *,
    refresh: TokenRefresher | None = None,
    **kwargs,
) -> requests.Response:
    """GET ``url`` and, on a 401, refresh the OAuth token once and retry.

    ``refresh`` (when provided) mints a fresh access token — the mid-run
    reactive refresh that lets a load outlive a short-lived OAuth token
    (arch #252, finding 14#3). It is consulted only on a 401 (an expiry
    signal); a 403 is a permission error left to the caller. On refresh
    failure the original 401 response is returned so the caller raises its
    provider ``AuthError`` (fail closed — never a stale retry).
    """
    resp = session.get(url, **kwargs)
    if resp.status_code != 401 or refresh is None:
        return resp
    try:
        new_token = refresh()
    except Exception:
        logger.warning("Mid-run token refresh failed; surfacing auth error", exc_info=True)
        return resp
    if not new_token:
        return resp
    session.headers["Authorization"] = f"Bearer {new_token}"
    return session.get(url, **kwargs)
