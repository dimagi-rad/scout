"""Shared utilities for CommCare HQ API loaders.

All loaders should use CommCareBaseLoader as a base class so they share
a single requests.Session (HTTP connection pooling), consistent timeouts,
and a single auth-header builder.
"""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter

from apps.common.errors import (
    CommCareAccessDeniedError,
    CommCareAuthError,  # noqa: F401  — re-exported; callers catch the provider base
    CommCareTokenExpiredError,
)
from mcp_server.loaders._http import build_retry, get_with_auth_refresh
from mcp_server.loaders._urls import ProviderURLPolicy

logger = logging.getLogger(__name__)

# (connect_timeout_seconds, read_timeout_seconds)
# Read timeout is generous: large CommCare domains may have slow API responses.
HTTP_TIMEOUT: tuple[int, int] = (10, 120)


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
        # A mid-run token refresher (OAuth only); None for API keys, which never
        # expire mid-run (arch #252, finding 14#3).
        self._refresh = credential.get("refresh")
        self._session = requests.Session()
        self._session.headers.update(build_auth_header(credential))
        adapter = HTTPAdapter(max_retries=build_retry())
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _resolve_next_url(self, base_url: str, next_url: str | None) -> str | None:
        """Resolve a next link against the last page, enforcing the HQ origin."""
        if not next_url:
            return None
        return ProviderURLPolicy("https://www.commcarehq.org").resolve(
            next_url, relative_to=getattr(self, "_response_url", base_url)
        )

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET a URL, raising on auth failure or an unrecoverable status.

        Transient 5xx/429 responses are retried by the session adapter
        (bounded, capped Retry-After); a surviving non-2xx becomes a typed
        error rather than a bare ``requests.HTTPError``.
        """
        resp = get_with_auth_refresh(
            self._session,
            url,
            trusted_origin="https://www.commcarehq.org",
            refresh=self._refresh,
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        self._response_url = resp.url if isinstance(resp.url, str) else url
        # Describe only; remediation copy belongs to the presentation layer, keyed
        # off the ErrorCode these classes carry (rule 3, apps/common/errors.py).
        if resp.status_code == 403:
            raise CommCareAccessDeniedError(
                f"CommCare HQ denied access to project space {self.domain} "
                f"(HTTP 403). The sign-in is still valid and has no access to that "
                f"project space — the membership or API access for it may have been "
                f"removed."
            )
        if resp.status_code == 401:
            raise CommCareTokenExpiredError(
                f"CommCare HQ rejected the sign-in for project space {self.domain} "
                f"(HTTP 401): it has expired or been revoked."
            )
        if resp.status_code >= 400:
            raise CommCareExportError(
                f"CommCare export request failed for domain {self.domain}: HTTP {resp.status_code}"
            )
        return resp

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET a URL and parse JSON, raising CommCareExportError on invalid JSON."""
        resp = self._get(url, params=params)
        try:
            return resp.json()
        except ValueError as e:
            raise CommCareExportError(f"CommCare API returned invalid JSON: {e}") from e
