"""
Tenant resolution for OAuth providers.

After a user authenticates, this service queries the provider's API
to discover which tenants (domains/organizations) the user belongs to,
and stores them as TenantMembership records.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

from apps.users.models import Tenant, TenantCredential, TenantMembership

logger = logging.getLogger(__name__)

COMMCARE_DOMAIN_API = "https://www.commcarehq.org/api/user_domains/v1/"


class CommCareAuthError(Exception):
    """Raised when CommCare returns a 401/403 during domain resolution."""


class ConnectAuthError(Exception):
    """Raised when Connect returns a 401/403 during opportunity resolution."""


class OCSAuthError(Exception):
    """Raised when OCS returns a 401/403 during chatbot resolution."""


async def resolve_commcare_domains(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's CommCare domains and upsert TenantMembership records."""
    domains = await _fetch_all_domains(access_token)
    memberships = []

    for domain in domains:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="commcare",
            external_id=domain["domain_name"],
            defaults={"canonical_name": domain["project_name"]},
        )
        tm, _ = await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
        await TenantCredential.objects.aget_or_create(
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


async def resolve_connect_opportunities(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's Connect opportunities and upsert TenantMembership records."""
    try:
        from django.conf import settings

        base_url = getattr(settings, "CONNECT_API_URL", "https://connect.dimagi.com")
    except ImportError:
        base_url = "https://connect.dimagi.com"

    url = f"{base_url.rstrip('/')}/export/opp_org_program_list/"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code in (401, 403):
        raise ConnectAuthError(
            f"Connect returned {resp.status_code} — access token may have expired"
        )
    resp.raise_for_status()

    opportunities = resp.json().get("opportunities", [])
    memberships = []

    for opp in opportunities:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="commcare_connect",
            external_id=str(opp["id"]),
            defaults={"canonical_name": opp["name"]},
        )
        tm, _ = await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
        await TenantCredential.objects.aget_or_create(
            tenant_membership=tm,
            defaults={"credential_type": TenantCredential.OAUTH},
        )
        memberships.append(tm)

    logger.info(
        "Resolved %d Connect opportunities for user %s",
        len(memberships),
        user.email,
    )
    return memberships


async def resolve_ocs_chatbots(user, access_token: str, team_id: str | None = None) -> list[TenantMembership]:
    """Fetch the user's OCS chatbots (experiments) and upsert TenantMembership records.

    OCS tokens are team-scoped — every experiment returned belongs to the team
    the user selected during OAuth consent.

    Args:
        user: The user to associate memberships with
        access_token: The OCS OAuth access token
        team_id: Optional team identifier. If not provided, a team context is fetched
                 from the OCS API (e.g., the workspace the token is scoped to).
    """
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")
    url: str | None = f"{base_url}/api/experiments/"
    headers = {"Authorization": f"Bearer {access_token}"}

    # If team_id is not provided, try to fetch it from OCS team/workspace endpoint
    if team_id is None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Try to fetch workspace/team context from OCS
                team_resp = await client.get(f"{base_url}/api/teams/", headers=headers)
                if team_resp.status_code == 200:
                    team_data = team_resp.json()
                    # OCS may return a list; if so, use the first team
                    if isinstance(team_data, list) and team_data:
                        team_id = str(team_data[0].get("id"))
                    elif isinstance(team_data, dict) and "id" in team_data:
                        team_id = str(team_data["id"])
        except Exception:
            # If team endpoint doesn't exist or fails, continue without team_id
            logger.debug("Failed to fetch OCS team context; continuing without team_id")
            team_id = None

    experiments: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise OCSAuthError(
                    f"OCS returned {resp.status_code} — access token may have expired"
                )
            resp.raise_for_status()
            payload = resp.json()
            experiments.extend(payload.get("results", []))
            url = payload.get("next")

    memberships = []
    for exp in experiments:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="ocs",
            external_id=str(exp["id"]),
            defaults={"canonical_name": exp.get("name") or str(exp["id"])},
        )
        tm, _ = await TenantMembership.objects.aget_or_create(user=user, tenant=tenant)
        # For OAuth, scope to credential_type to avoid matching/corrupting API-key rows
        await TenantCredential.objects.aupdate_or_create(
            tenant_membership=tm,
            credential_type=TenantCredential.OAUTH,
            team_id="",  # OAuth credentials have empty team_id
        )
        memberships.append(tm)

    logger.info(
        "Resolved %d OCS chatbots for user %s (team_id: %s)",
        len(memberships),
        user.email,
        team_id,
    )
    return memberships


async def _fetch_all_domains(access_token: str) -> list[dict]:
    """Paginate through the CommCare user_domains API.

    Raises CommCareAuthError on 401/403 so callers can distinguish an
    expired token from a generic server error.
    """
    results = []
    url = COMMCARE_DOMAIN_API
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code in (401, 403):
                raise CommCareAuthError(
                    f"CommCare returned {resp.status_code} — access token may have expired"
                )
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("objects", []))
            next_url = data.get("meta", {}).get("next")
            if next_url and next_url.startswith(COMMCARE_DOMAIN_API.split("/api/")[0]):
                url = next_url
            else:
                url = None
    return results
