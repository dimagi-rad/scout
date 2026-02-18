"""CommCare case loader — fetches case data from the CommCare HQ REST API."""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareCaseLoader:
    """Loads case records from CommCare HQ for a given domain."""

    def __init__(self, domain: str, access_token: str):
        self.domain = domain
        self.access_token = access_token
        self.base_url = f"{COMMCARE_API_BASE}/a/{domain}/api/v0.5/case/"

    def load(self) -> list[dict]:
        """Fetch all cases from the CommCare API (paginated)."""
        results = []
        url = self.base_url
        params = {"format": "json", "limit": 100}

        while url:
            resp = requests.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("objects", []))

            url = data.get("meta", {}).get("next")
            params = {}  # next URL includes params

            logger.info(
                "Loaded %d/%d cases for domain %s",
                len(results),
                data.get("meta", {}).get("total_count", "?"),
                self.domain,
            )

        return results
