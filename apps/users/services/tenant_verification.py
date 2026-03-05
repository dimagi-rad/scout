"""Verify provider credentials before creating Tenant records."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

COMMCARE_API_BASE = "https://www.commcarehq.org"


class CommCareVerificationError(Exception):
    """Raised when CommCare credential verification fails."""


def verify_commcare_credential(domain: str, username: str, api_key: str) -> dict:
    """Verify a CommCare API key against the CommCare web-user API.

    Calls GET /a/{domain}/api/v0.5/web-user/{username}/ with the supplied
    API key. Returns the user info dict on success.

    Raises CommCareVerificationError if the credential is invalid, the user
    doesn't exist, or the user is not a member of the domain.
    """
    url = f"{COMMCARE_API_BASE}/a/{domain}/api/v0.5/web-user/{username}/"
    resp = requests.get(
        url,
        headers={"Authorization": f"ApiKey {username}:{api_key}"},
        timeout=15,
    )
    if resp.status_code in (401, 403):
        raise CommCareVerificationError(
            f"CommCare rejected the API key for domain '{domain}' (HTTP {resp.status_code})"
        )
    if resp.status_code == 404:
        raise CommCareVerificationError(f"User '{username}' not found in domain '{domain}'")
    if not resp.ok:
        raise CommCareVerificationError(
            f"CommCare API returned unexpected status {resp.status_code}"
        )
    return resp.json()
