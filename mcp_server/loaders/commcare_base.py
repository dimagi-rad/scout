"""Shared utilities for CommCare HQ API loaders.

All loaders should use CommCareBaseLoader as a base class so they share
a single requests.Session (HTTP connection pooling), consistent timeouts,
and a single auth-header builder.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter

from mcp_server.loaders._http import build_retry

logger = logging.getLogger(__name__)

# (connect_timeout_seconds, read_timeout_seconds)
# Read timeout is generous: large CommCare domains may have slow API responses.
HTTP_TIMEOUT: tuple[int, int] = (10, 120)


class CommCareAuthError(Exception):
    """Raised when CommCare returns a 401 or 403 response."""


class CommCareExportError(Exception):
    """Raised on unrecoverable CommCare export failures.

    Covers malformed responses (invalid JSON, a missing page-collection key)
    and non-2xx responses that survive the retry policy. CommCare HQ actively
    rate-limits its APIs (HTTP 429 + Retry-After by design), so a bare
    ``raise_for_status`` with no retry turned an expected throttle into a run
    failure (arch #252, findings 12#4/03#6).
    """


def build_auth_header(credential: dict[str, str]) -> dict[str, str]:
    """Return the Authorization header dict for a credential.

    Args:
        credential: {"type": "oauth"|"api_key", "value": str}
    """
    if credential.get("type") == "api_key":
        return {"Authorization": f"ApiKey {credential['value']}"}
    return {"Authorization": f"Bearer {credential['value']}"}


class CommCareBaseLoader:
    """Base class for CommCare HQ API loaders.

    Manages a persistent requests.Session (HTTP connection pooling) and
    applies consistent timeouts and auth headers to every request.
    """

    def __init__(self, domain: str, credential: dict[str, str]) -> None:
        self.domain = domain
        self._session = requests.Session()
        self._session.headers.update(build_auth_header(credential))
        adapter = HTTPAdapter(max_retries=build_retry())
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _resolve_next_url(self, base_url: str, next_url: str | None) -> str | None:
        """Resolve a potentially-relative ``next`` URL from a CommCare API response.

        CommCare APIs return ``meta.next`` in several formats:
        - Absolute URL (e.g. ``https://www.commcarehq.org/a/domain/api/...``) — returned as-is.
        - Path-relative (e.g. ``/a/domain/api/...``) — resolved against the base URL.
        - Query-string-only (e.g. ``?limit=1000&offset=1000``) — resolved against the base URL.
        """
        if not next_url:
            return None
        return urljoin(base_url, next_url)

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET a URL, raising on auth failure or an unrecoverable status.

        Transient 5xx/429 responses are retried by the session adapter
        (bounded, capped Retry-After); a surviving non-2xx becomes a typed
        error rather than a bare ``requests.HTTPError``.
        """
        resp = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise CommCareAuthError(
                f"CommCare authentication failed for domain {self.domain} "
                f"(HTTP {resp.status_code}). Your CommCare sign-in has likely expired "
                f"or been revoked — please reconnect your CommCare account and retry."
            )
        if resp.status_code >= 400:
            raise CommCareExportError(
                f"CommCare export request failed for domain {self.domain}: "
                f"HTTP {resp.status_code} for {url}"
            )
        return resp

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET a URL and parse JSON, raising CommCareExportError on invalid JSON."""
        resp = self._get(url, params=params)
        try:
            return resp.json()
        except ValueError as e:
            raise CommCareExportError(f"CommCare API returned invalid JSON for {url}: {e}") from e
