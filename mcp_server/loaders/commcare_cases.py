"""CommCare case loader — fetches case data from the CommCare HQ Case API v2."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareAuthError(Exception):
    """Raised when the CommCare API rejects the credential (401/403)."""


class CommCareCaseLoader:
    """Loads case records from CommCare HQ using the Case API v2.

    The v2 API uses cursor-based pagination and returns cases serialized with
    fields like case_name, last_modified, indices, and properties.

    Args:
        domain: CommCare domain name.
        credential: Dict with keys "type" ("oauth" or "api_key") and "value".
            For oauth: value is a Bearer token string.
            For api_key: value is "username:apikey" string.

    See: https://commcare-hq.readthedocs.io/api/cases-v2.html
    """

    def __init__(
        self,
        domain: str,
        credential: dict[str, str] | None = None,
        *,
        page_size: int = 1000,
        # Legacy parameter kept for backwards compatibility
        access_token: str | None = None,
    ):
        self.domain = domain
        if credential is None:
            credential = {}
        if access_token is not None and not credential:
            # Legacy callers: wrap plain token as oauth credential
            credential = {"type": "oauth", "value": access_token}
        self.credential = credential
        self.page_size = min(page_size, 5000)  # API max is 5000
        self.base_url = f"{COMMCARE_API_BASE}/a/{domain}/api/case/v2/"

    def _auth_header(self) -> str:
        cred_type = self.credential.get("type", "oauth")
        value = self.credential.get("value", "")
        if cred_type == "api_key":
            return f"ApiKey {value}"
        return f"Bearer {value}"

    def load(self) -> list[dict]:
        """Fetch all cases from the CommCare Case API v2 (cursor-paginated)."""
        results: list[dict] = []
        url = self.base_url
        params = {"limit": self.page_size}

        while url:
            resp = requests.get(
                url,
                params=params,
                headers={"Authorization": self._auth_header()},
                timeout=60,
            )
            if resp.status_code in (401, 403):
                raise CommCareAuthError(
                    f"CommCare returned {resp.status_code} — the credential may be "
                    f"expired or invalid. Please reconnect your CommCare account."
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
