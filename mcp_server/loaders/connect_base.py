"""Shared utilities for CommCare Connect API loaders.

All Connect loaders should use ConnectBaseLoader as a base class so they share
a single requests.Session (HTTP connection pooling), consistent timeouts,
and a single auth-header builder.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter

from mcp_server.loaders._http import (
    RETRY_STATUS_FORCELIST,
    RETRY_TOTAL,
    build_retry,
)

logger = logging.getLogger(__name__)

# (connect_timeout, read_timeout). Each paginated page is bounded server-side
# (~1000 records), so per-request reads are well under 60s. The 300s read
# timeout is preserved for the metadata endpoints, which are not paginated.
HTTP_TIMEOUT: tuple[int, int] = (10, 300)

# Versioned Accept header for the v2 paginated JSON export endpoints.
# Sent per-call (not session-global) so non-versioned endpoints — e.g.
# `/export/opp_org_program_list/` used by ConnectMetadataLoader — are
# unaffected.
EXPORT_ACCEPT_HEADER = "application/json; version=2.0"


def _extract_last_id(url: str, params: dict | None) -> int | None:
    """Best-effort recovery of the cursor value at the time of failure.

    On the first page, the cursor (if any) is in ``params``; on subsequent
    pages the server-built ``next`` URL embeds it as a query string.
    """
    if params and "last_id" in params:
        try:
            return int(params["last_id"])
        except (TypeError, ValueError):
            return None
    qs = parse_qs(urlparse(url).query)
    values = qs.get("last_id")
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


class ConnectAuthError(Exception):
    """Raised when Connect returns a 401 or 403 response."""


class ConnectExportError(Exception):
    """Raised on unrecoverable Connect export failures.

    Covers both malformed responses (missing ``results`` key, invalid JSON)
    and non-2xx responses that survived the retry policy. When raised after
    retry exhaustion, ``status``, ``attempts``, ``sentry_trace``, and
    ``last_id`` are populated so the materializer can log structured context
    and operators can correlate with upstream traces.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        sentry_trace: str | None = None,
        attempts: int = 1,
        last_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.sentry_trace = sentry_trace
        self.attempts = attempts
        self.last_id = last_id


class ConnectBaseLoader:
    """Base class for Connect API loaders.

    Manages a persistent requests.Session with OAuth Bearer token auth
    and provides helpers for paginated v2 JSON exports and ad-hoc JSON GETs.
    """

    DEFAULT_BASE_URL = "https://connect.dimagi.com"

    def __init__(
        self,
        opportunity_id: int,
        credential: dict[str, str],
        base_url: str | None = None,
    ) -> None:
        self.opportunity_id = opportunity_id
        if base_url is None:
            try:
                from django.conf import settings

                base_url = getattr(settings, "CONNECT_API_URL", self.DEFAULT_BASE_URL)
            except ImportError:
                base_url = self.DEFAULT_BASE_URL
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {credential['value']}"})
        adapter = HTTPAdapter(max_retries=build_retry())
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET a URL, raising ConnectAuthError on 401/403."""
        resp = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise ConnectAuthError(
                f"Connect auth failed for opportunity {self.opportunity_id}: "
                f"HTTP {resp.status_code}"
            )
        resp.raise_for_status()
        return resp

    def _opp_url(self, suffix: str) -> str:
        """Build a URL for an opportunity-scoped endpoint."""
        return f"{self.base_url}/export/opportunity/{self.opportunity_id}/{suffix}"

    def _paginate_export_pages(
        self,
        suffix: str,
        params: dict | None = None,
        start_last_id: int | None = None,
    ) -> Iterator[tuple[list[dict], int | None]]:
        """Yield ``(page, total_count)`` from a v2 paginated JSON export endpoint.

        Calls ``_opp_url(suffix)`` first, then follows the server-provided
        ``next`` URL until it is null. ``params`` are sent only on the first
        request — the ``next`` URL already includes preserved query params.

        When ``start_last_id`` is provided, the initial request includes
        ``last_id=<start_last_id>`` so Connect's keyset pagination resumes
        with records strictly greater than that id (forward order). This
        supports the resumable-materialization path in issue #187.

        Each yielded ``page`` is the ``results`` list from one response
        (bounded server-side, default ~1000 records). ``total_count`` is the
        ``count`` field from the first response only; subsequent pages yield
        ``None`` to avoid re-reading it. Empty result lists are yielded as
        ``[]`` so callers can rely on the loop terminating naturally.

        Raises:
            ConnectAuthError: on 401/403.
            ConnectExportError: when the response is not valid JSON, is
                missing the ``results`` key, or returns a non-2xx status
                that survives the configured retry policy. On retry
                exhaustion, ``status``, ``attempts``, ``sentry_trace``, and
                ``last_id`` are populated for structured logging.
        """
        url: str | None = self._opp_url(suffix)
        request_params: dict | None = dict(params) if params else None
        if start_last_id is not None:
            request_params = request_params or {}
            request_params["last_id"] = start_last_id
        headers = {"Accept": EXPORT_ACCEPT_HEADER}
        first_page = True

        while url is not None:
            # NOTE: relies on requests' default ``allow_redirects=True``.
            # Production CommCare Connect has been observed returning
            # ``next`` URLs with the ``http://`` scheme even when the
            # caller used HTTPS — see dimagi/commcare-connect#1109. The
            # edge layer 301-redirects http→https; ``requests`` follows
            # the redirect and preserves the Authorization header on
            # same-host upgrades. See test_follows_http_to_https_redirect
            # _on_next_url for the regression pin.
            resp = self._session.get(
                url, params=request_params, headers=headers, timeout=HTTP_TIMEOUT
            )
            if resp.status_code in (401, 403):
                raise ConnectAuthError(
                    f"Connect auth failed for opportunity {self.opportunity_id}: "
                    f"HTTP {resp.status_code}"
                )
            if not resp.ok:
                # A status in the forcelist means the urllib3 Retry policy
                # ran to exhaustion (RETRY_TOTAL retries on top of the initial
                # attempt). Anything else short-circuited on the first try.
                attempts = RETRY_TOTAL + 1 if resp.status_code in RETRY_STATUS_FORCELIST else 1
                raise ConnectExportError(
                    f"Connect export request failed for opportunity "
                    f"{self.opportunity_id}: HTTP {resp.status_code} for {url}",
                    status=resp.status_code,
                    sentry_trace=resp.headers.get("sentry-trace"),
                    attempts=attempts,
                    last_id=_extract_last_id(url, request_params),
                )

            try:
                payload = resp.json()
            except ValueError as e:
                raise ConnectExportError(f"Export API returned invalid JSON for {url}: {e}") from e

            if "results" not in payload:
                raise ConnectExportError(f"Export API response missing 'results' key for {url}")

            if first_page:
                total = payload.get("count")
                if not isinstance(total, int):
                    total = None
                yield payload["results"], total
                first_page = False
            else:
                yield payload["results"], None

            url = payload.get("next")
            # The server's `next` URL already preserves all original params
            # (last_id, page_size, order, plus any caller-supplied filters).
            request_params = None
