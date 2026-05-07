"""Shared utilities for Open Chat Studio API loaders."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT: tuple[int, int] = (10, 300)


class OCSAuthError(Exception):
    """Raised when OCS returns a 401 or 403 response."""


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

    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        resp = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise OCSAuthError(
                f"OCS auth failed for experiment {self.experiment_id}: HTTP {resp.status_code}"
            )
        resp.raise_for_status()
        return resp

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
            resp = self._session.get(current_url, params=current_params, timeout=HTTP_TIMEOUT)
            if resp.status_code in (401, 403):
                raise OCSAuthError(
                    f"OCS auth failed for experiment {self.experiment_id}: HTTP {resp.status_code}"
                )
            resp.raise_for_status()
            payload = resp.json()
            page = payload.get("results", [])
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
