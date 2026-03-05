"""Verify provider credentials before creating Tenant records."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareVerificationError(Exception):
    """Raised when CommCare credential verification fails."""


def verify_commcare_credential(domain: str, username: str, api_key: str) -> dict:
    """Verify a CommCare API key using the user domain list API.

    Calls GET /api/user_domains/v1/ with the supplied API key and checks that
    the specified domain appears in the returned list of domains.

    Returns a dict with domain info on success.

    Raises CommCareVerificationError if the credential is invalid or the user
    is not a member of the domain.
    """
    url = f"{COMMCARE_API_BASE}/api/user_domains/v1/"
    resp = requests.get(
        url,
        headers={"Authorization": f"ApiKey {username}:{api_key}"},
        timeout=15,
    )
    if resp.status_code in (401, 403):
        raise CommCareVerificationError(f"CommCare rejected the API key (HTTP {resp.status_code})")
    if not resp.ok:
        logger.warning(
            "CommCare verification failed: username=%s status=%s body=%s",
            username,
            resp.status_code,
            resp.text[:500],
        )
        raise CommCareVerificationError(
            f"CommCare API returned unexpected status {resp.status_code}"
        )
    data = resp.json()
    for entry in data.get("objects", []):
        if entry.get("domain_name") == domain:
            return entry
    raise CommCareVerificationError(f"User '{username}' is not a member of domain '{domain}'")
