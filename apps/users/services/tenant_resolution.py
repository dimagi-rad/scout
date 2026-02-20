"""
Tenant resolution for OAuth providers.

After a user authenticates, this service queries the provider's API
to discover which tenants (domains/organizations) the user belongs to,
and stores them as TenantMembership records.
"""

from __future__ import annotations

import logging

import requests

from apps.users.models import TenantCredential, TenantMembership

logger = logging.getLogger(__name__)

COMMCARE_DOMAIN_API = "https://www.commcarehq.org/api/user_domains/v1/"


def resolve_commcare_domains(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's CommCare domains and upsert TenantMembership records."""
    domains = _fetch_all_domains(access_token)
    memberships = []

    for domain in domains:
        tm, _created = TenantMembership.objects.update_or_create(
            user=user,
            provider="commcare",
            tenant_id=domain["domain_name"],
            defaults={"tenant_name": domain["project_name"]},
        )
        # Ensure a TenantCredential(oauth) exists for this membership
        TenantCredential.objects.get_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        memberships.append(tm)

    logger.info(
        "Resolved %d CommCare domains for user %s",
        len(memberships),
        user.email,
    )
    return memberships


class CommCareAuthError(Exception):
    """Raised when CommCare returns a 401/403 during domain resolution."""


def _fetch_all_domains(access_token: str) -> list[dict]:
    """Paginate through the CommCare user_domains API.

    Raises CommCareAuthError on 401/403 so callers can distinguish an
    expired token from a generic server error.
    """
    results = []
    url = COMMCARE_DOMAIN_API
    while url:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        if resp.status_code in (401, 403):
            raise CommCareAuthError(
                f"CommCare returned {resp.status_code} — access token may have expired"
            )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("objects", []))
        next_url = data.get("meta", {}).get("next")
        # Only follow next URLs that point to the same host (SSRF protection)
        if next_url and next_url.startswith(COMMCARE_DOMAIN_API.split("/api/")[0]):
            url = next_url
        else:
            url = None
    return results
