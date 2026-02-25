"""Loader for CommCare case records (Case API v2)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from mcp_server.loaders.commcare_base import CommCareAuthError, CommCareBaseLoader  # noqa: F401

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.commcarehq.org"
_DEFAULT_PAGE_SIZE = 1000


class CommCareCaseLoader(CommCareBaseLoader):
    """Loads CommCare case records from the Case API v2.

    Supports both ``load()`` (returns a flat list) and ``load_pages()``
    (yields one page at a time for streaming writes).
    """

    def __init__(
        self,
        domain: str,
        credential: dict[str, str] | None = None,
        access_token: str | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> None:
        # Support legacy ``access_token`` kwarg for backwards compatibility.
        if credential is None and access_token is not None:
            credential = {"type": "oauth", "value": access_token}
        elif credential is None:
            raise ValueError("Either credential or access_token is required")
        super().__init__(domain=domain, credential=credential)
        self.page_size = min(page_size, _DEFAULT_PAGE_SIZE)

    def load_pages(self) -> Iterator[list[dict]]:
        """Yield one page of cases at a time.

        Each page is a list of normalised case dicts. Prefer this over
        ``load()`` when writing to the DB to avoid holding all cases in memory.
        """
        url = f"{_BASE_URL}/a/{self.domain}/api/case/v2/"
        params: dict = {"limit": self.page_size}
        total_loaded = 0
        while url:
            data = self._get(url, params=params).json()
            cases = [_normalize_case(c) for c in data.get("cases", [])]
            if cases:
                total_loaded += len(cases)
                logger.info(
                    "Fetched %d cases (total so far: %d) for domain %s",
                    len(cases),
                    total_loaded,
                    self.domain,
                )
                yield cases
            url = data.get("next")
            params = {}

    def load(self) -> list[dict]:
        """Return all cases as a flat list (loads all pages into memory)."""
        return [case for page in self.load_pages() for case in page]


def _normalize_case(raw: dict) -> dict:
    return {
        "case_id": raw.get("case_id", ""),
        "case_type": raw.get("case_type", ""),
        "case_name": raw.get("case_name") or raw.get("properties", {}).get("case_name", ""),
        "external_id": raw.get("external_id", ""),
        "owner_id": raw.get("owner_id", ""),
        "date_opened": raw.get("date_opened", ""),
        "last_modified": raw.get("last_modified", ""),
        "server_last_modified": raw.get("server_last_modified", ""),
        "indexed_on": raw.get("indexed_on", ""),
        "closed": raw.get("closed", False),
        "date_closed": raw.get("date_closed") or "",
        "properties": raw.get("properties", {}),
        "indices": raw.get("indices", {}),
    }
