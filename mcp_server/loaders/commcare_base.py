"""Shared utilities for CommCare HQ API loaders.

All loaders should use CommCareBaseLoader as a base class so they share
a single requests.Session (HTTP connection pooling), consistent timeouts,
and a single auth-header builder.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

# (connect_timeout_seconds, read_timeout_seconds)
# Read timeout is generous: large CommCare domains may have slow API responses.
HTTP_TIMEOUT: tuple[int, int] = (10, 120)


class CommCareAuthError(Exception):
    """Raised when CommCare returns a 401 or 403 response."""


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
        """GET a URL, raising CommCareAuthError on 401/403."""
        resp = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise CommCareAuthError(
                f"CommCare auth failed for domain {self.domain}: HTTP {resp.status_code}"
            )
        resp.raise_for_status()
        return resp
