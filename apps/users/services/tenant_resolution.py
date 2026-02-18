"""
Tenant resolution for OAuth providers.

After a user authenticates, this service queries the provider's API
to discover which tenants (domains/organizations) the user belongs to,
and stores them as TenantMembership records.
"""
from __future__ import annotations

import logging

import requests

from apps.users.models import TenantMembership

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
        memberships.append(tm)

    logger.info(
        "Resolved %d CommCare domains for user %s",
        len(memberships),
        user.email,
    )
    return memberships


def _fetch_all_domains(access_token: str) -> list[dict]:
    """Paginate through the CommCare user_domains API."""
    results = []
    url = COMMCARE_DOMAIN_API
    while url:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("objects", []))
        url = data.get("meta", {}).get("next")
    return results
