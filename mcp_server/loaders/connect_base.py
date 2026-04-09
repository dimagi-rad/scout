"""Shared utilities for CommCare Connect API loaders.

All Connect loaders should use ConnectBaseLoader as a base class so they share
a single requests.Session (HTTP connection pooling), consistent timeouts,
and a single auth-header builder.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import requests

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


class ConnectAuthError(Exception):
    """Raised when Connect returns a 401 or 403 response."""


class ConnectExportError(Exception):
    """Raised when a v2 export response is malformed (missing 'results', invalid JSON)."""


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
    ) -> Iterator[list[dict]]:
        """Yield pages of records from a v2 paginated JSON export endpoint.

        Calls ``_opp_url(suffix)`` first, then follows the server-provided
        ``next`` URL until it is null. ``params`` are sent only on the first
        request — the ``next`` URL already includes preserved query params.

        Each yielded page is the ``results`` list from one response (bounded
        server-side, default ~1000 records). Empty result lists are yielded
        as ``[]`` so callers can rely on the loop terminating naturally.

        Raises:
            ConnectAuthError: on 401/403.
            ConnectExportError: when the response is not valid JSON or is
                missing the ``results`` key.
            requests.HTTPError: on any other non-2xx response.
        """
        url: str | None = self._opp_url(suffix)
        request_params: dict | None = params
        headers = {"Accept": EXPORT_ACCEPT_HEADER}

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
            resp.raise_for_status()

            try:
                payload = resp.json()
            except ValueError as e:
                raise ConnectExportError(f"Export API returned invalid JSON for {url}: {e}") from e

            if "results" not in payload:
                raise ConnectExportError(f"Export API response missing 'results' key for {url}")

            yield payload["results"]

            url = payload.get("next")
            # The server's `next` URL already preserves all original params
            # (last_id, page_size, order, plus any caller-supplied filters).
            request_params = None
