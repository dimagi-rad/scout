"""Shared utilities for Open Chat Studio API loaders."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter

from mcp_server.loaders._http import build_retry

logger = logging.getLogger(__name__)

HTTP_TIMEOUT: tuple[int, int] = (10, 300)

# OCS list endpoints default to ``page_size=100`` (DRF ``CursorPagination``) but
# support up to ``max_page_size=1500`` via a ``page_size`` query param. Sending
# the max cuts list-request volume ~10-15x versus the upstream default — the
# loaders' Connect/CommCare mental model is 1000/page (arch #254, finding 13#1).
OCS_MAX_PAGE_SIZE = 1500


class OCSAuthError(Exception):
    """Raised when OCS returns a 401 or 403 response."""


class OCSExportError(Exception):
    """Raised on unrecoverable OCS export failures.

    Covers invalid JSON, a missing ``results`` key, and non-2xx responses that
    survive the retry policy. Mirrors ConnectExportError so a malformed
    response fails loudly instead of yielding a silently-empty page
    (arch #252, finding 03#6).
    """


class OCSBaseLoader:
    """Base class for Open Chat Studio API loaders.

    OCS uses cursor-based pagination: responses contain ``{"results": [...],
    "next": <absolute url or null>}``.
    """

    DEFAULT_BASE_URL = "https://www.openchatstudio.com"

    def __init__(
        self,
        experiment_id: str,
        credential: dict[str, str],
        base_url: str | None = None,
    ) -> None:
        self.experiment_id = experiment_id
        if base_url is None:
            base_url = getattr(settings, "OCS_URL", self.DEFAULT_BASE_URL)
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        if credential.get("type") == "api_key":
            self._session.headers.update({"X-api-key": credential["value"]})
        else:
            self._session.headers.update({"Authorization": f"Bearer {credential['value']}"})
        adapter = HTTPAdapter(max_retries=build_retry())
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET a URL, raising on auth failure or an unrecoverable status.

        Transient 5xx/429 responses are retried by the session adapter; a
        surviving non-2xx becomes a typed error rather than a bare HTTPError.
        """
        resp = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise OCSAuthError(
                f"OCS authentication failed for experiment {self.experiment_id} "
                f"(HTTP {resp.status_code}). Your Open Chat Studio sign-in has likely "
                f"expired or been revoked — please reconnect your account and retry."
            )
        if resp.status_code >= 400:
            raise OCSExportError(
                f"OCS export request failed for experiment {self.experiment_id}: "
                f"HTTP {resp.status_code} for {url}"
            )
        return resp

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET a URL and parse JSON, raising OCSExportError on invalid JSON."""
        resp = self._get(url, params=params)
        try:
            return resp.json()
        except ValueError as e:
            raise OCSExportError(f"OCS API returned invalid JSON for {url}: {e}") from e

    def _paginate(
        self, url: str, params: dict | None = None
    ) -> Iterator[tuple[list[dict], int | None]]:
        """Yield ``(page, total_count)`` from a cursor-paginated endpoint.

        ``total_count`` is read from the ``count`` field in the first
        response's envelope (when present) and yielded only with the first
        page; subsequent pages yield ``(page, None)``. OCS' cursor-based
        pagination may omit ``count`` — callers must tolerate ``None``.
        """
        current_url: str | None = url
        current_params: dict | None = params
        first_page = True
        while current_url:
            payload = self._get_json(current_url, params=current_params)
            if "results" not in payload:
                # A missing ``results`` key means the envelope changed — fail
                # rather than yielding a silently-empty page (finding 03#6).
                raise OCSExportError(f"OCS API response missing 'results' key for {current_url}")
            page = payload["results"]
            if first_page:
                total = payload.get("count")
                if not isinstance(total, int):
                    total = None
                yield page, total
                first_page = False
            else:
                yield page, None
            current_url = payload.get("next")
            current_params = None
