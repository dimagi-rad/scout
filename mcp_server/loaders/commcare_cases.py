"""CommCare case loader — fetches case data from the CommCare HQ Case API v2."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareAuthError(Exception):
    """Raised when the CommCare API rejects the OAuth token (401/403)."""


class CommCareCaseLoader:
    """Loads case records from CommCare HQ using the Case API v2.

    The v2 API uses cursor-based pagination and returns cases serialized with
    fields like case_name, last_modified, indices, and properties.

    See: https://commcare-hq.readthedocs.io/api/cases-v2.html
    """

    def __init__(self, domain: str, access_token: str, *, page_size: int = 1000):
        self.domain = domain
        self.access_token = access_token
        self.page_size = min(page_size, 5000)  # API max is 5000
        self.base_url = f"{COMMCARE_API_BASE}/a/{domain}/api/case/v2/"

    def load(self) -> list[dict]:
        """Fetch all cases from the CommCare Case API v2 (cursor-paginated)."""
        results: list[dict] = []
        url = self.base_url
        params = {"limit": self.page_size}

        while url:
            resp = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=60,
            )
            if resp.status_code in (401, 403):
                raise CommCareAuthError(
                    f"CommCare returned {resp.status_code} — the OAuth token may be "
                    f"expired or revoked. Please reconnect your CommCare account."
                )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("cases", []))

            # Cursor pagination: follow the "next" URL if present
            url = data.get("next")
            params = {}  # next URL includes all params

            logger.info(
                "Loaded %d/%s cases for domain %s",
                len(results),
                data.get("matching_records", "?"),
                self.domain,
            )

        return results
