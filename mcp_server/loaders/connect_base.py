"""Shared utilities for CommCare Connect API loaders.

All Connect loaders should use ConnectBaseLoader as a base class so they share
a single requests.Session (HTTP connection pooling), consistent timeouts,
and a single auth-header builder.
"""

from __future__ import annotations

import csv
import io
import logging

import requests

logger = logging.getLogger(__name__)

# (connect_timeout, read_timeout) — 300s read timeout for large CSV exports
HTTP_TIMEOUT: tuple[int, int] = (10, 300)


class ConnectAuthError(Exception):
    """Raised when Connect returns a 401 or 403 response."""


class ConnectBaseLoader:
    """Base class for Connect API loaders.

    Manages a persistent requests.Session with OAuth Bearer token auth
    and provides helpers for JSON and CSV endpoints.
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

    def _get_csv(self, url: str, params: dict | None = None) -> list[dict]:
        """GET a CSV endpoint and parse into a list of dicts."""
        resp = self._get(url, params=params)
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)

    def _opp_url(self, suffix: str) -> str:
        """Build a URL for an opportunity-scoped endpoint."""
        return f"{self.base_url}/export/opportunity/{self.opportunity_id}/{suffix}"
