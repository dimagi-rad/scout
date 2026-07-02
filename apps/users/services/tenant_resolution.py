"""
Tenant resolution for OAuth providers.

After a user authenticates (or a server-side refresh runs on their behalf), this
service queries the provider's API to discover which tenants (domains / opps /
chatbots) the user currently belongs to, and **full-syncs** the corresponding
TenantMembership records: new access is added/un-archived, and access the provider
no longer returns is archived (revoked).

Revocation safety — a fetch that silently returned a *partial* or *shape-drifted*
result would wrongly archive access the user still has, so every fetch either
returns a provably complete set or raises **before** any archival:
  * HTTP 401/403/non-2xx → raise (token expired / wrong scope).
  * missing expected key in a 2xx body → raise (never treat drift as "zero tenants").
  * CommCare pagination that can't be followed → raise (no silent truncation).
Callers (the login signal and ``tenant_list_view``) treat any raise as "skip
refresh," so access is never revoked on an inconclusive fetch (fail-open).
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

import httpx
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.utils import timezone

from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.ocs_team import adetect_team_name_from_oauth

logger = logging.getLogger(__name__)

COMMCARE_DOMAIN_API = "https://www.commcarehq.org/api/user_domains/v1/"


async def _ocs_team_slug(user) -> str:
    """The OCS team slug the user's current OAuth token is scoped to (OIDC claim)."""
    acct = await SocialAccount.objects.filter(user=user, provider="ocs").afirst()
    return (acct.extra_data or {}).get("team", "") if acct else ""


class CommCareAuthError(Exception):
    """Raised when CommCare returns a 401/403 during domain resolution."""


class ConnectAuthError(Exception):
    """Raised when Connect returns a 401/403 during opportunity resolution."""


class OCSAuthError(Exception):
    """Raised when OCS returns a 401/403 during chatbot resolution."""


class TenantResolutionError(Exception):
    """Raised when a provider returns a 2xx body of an unexpected shape.

    Treated like an auth error by callers (skip refresh) — the point is to abort
    before archival rather than mistake shape drift for "user has zero tenants."
    """


async def _sync_memberships(
    user,
    connection: TenantConnection,
    fresh_tenants: list[Tenant],
    *,
    membership_extra: dict | None = None,
    archive_team_slug: str | None = None,
) -> list[TenantMembership]:
    """Upsert memberships for ``fresh_tenants`` and archive this connection's stale ones.

    ``fresh_tenants`` must be the **complete** set the upstream fetch returned.
    Uses ``all_objects`` so a revoked tombstone is reused (un-archived) instead of
    colliding on ``unique(user, tenant)``. Archival is scoped to ``connection`` — so
    an OAuth refresh never touches an API-key connection's memberships or another
    provider's — and additionally to ``archive_team_slug`` for OCS, whose tokens are
    team-scoped (a successful fetch only covers the current team; other teams' access
    must be left intact). If a team-scoped provider has no resolvable team slug,
    archival is skipped entirely (additive only) since it can't be scoped safely.
    """
    fresh_ids: set = set()
    memberships: list[TenantMembership] = []
    for tenant in fresh_tenants:
        tm, _ = await TenantMembership.all_objects.aget_or_create(user=user, tenant=tenant)
        tm.connection = connection
        tm.archived_at = None
        fields = ["connection", "archived_at"]
        if membership_extra:
            for attr, val in membership_extra.items():
                setattr(tm, attr, val)  # team_slug/team_name setters mutate provider_metadata
            fields.append("provider_metadata")
        await tm.asave(update_fields=fields)
        memberships.append(tm)
        fresh_ids.add(tenant.id)

    archive_qs = TenantMembership.all_objects.filter(
        user=user, connection=connection, archived_at__isnull=True
    ).exclude(tenant_id__in=fresh_ids)
    if archive_team_slug is not None:
        if not archive_team_slug:
            return memberships  # team-scoped provider without a team → never revoke
        archive_qs = archive_qs.filter(provider_metadata__team_slug=archive_team_slug)
    await archive_qs.aupdate(archived_at=timezone.now())
    return memberships


async def resolve_commcare_domains(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's CommCare domains and full-sync TenantMembership records."""
    domains = await _fetch_all_domains(access_token)  # complete or raises
    conn, _ = await TenantConnection.objects.aget_or_create(
        user=user, provider="commcare", credential_type=TenantConnection.OAUTH
    )
    fresh = []
    for domain in domains:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="commcare",
            external_id=domain["domain_name"],
            defaults={"canonical_name": domain["project_name"]},
        )
        fresh.append(tenant)

    memberships = await _sync_memberships(user, conn, fresh)
    logger.info("Resolved %d CommCare domains for user %s", len(memberships), user.email)
    return memberships


async def resolve_connect_opportunities(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's Connect opportunities and full-sync TenantMembership records."""
    base_url = getattr(settings, "CONNECT_API_URL", "https://connect.dimagi.com")
    url = f"{base_url.rstrip('/')}/export/opp_org_program_list/"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code in (401, 403):
        raise ConnectAuthError(
            f"Connect returned {resp.status_code} — access token may have expired"
        )
    resp.raise_for_status()

    payload = resp.json()
    if "opportunities" not in payload:  # shape-drift guard — never archive on drift
        raise TenantResolutionError("Connect response missing 'opportunities' key")
    opportunities = payload["opportunities"]

    conn, _ = await TenantConnection.objects.aget_or_create(
        user=user, provider="commcare_connect", credential_type=TenantConnection.OAUTH
    )
    fresh = []
    for opp in opportunities:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="commcare_connect",
            external_id=str(opp["id"]),
            defaults={"canonical_name": opp["name"]},
        )
        fresh.append(tenant)

    memberships = await _sync_memberships(user, conn, fresh)
    logger.info("Resolved %d Connect opportunities for user %s", len(memberships), user.email)
    return memberships


async def resolve_ocs_chatbots(user, access_token: str) -> list[TenantMembership]:
    """Fetch the user's OCS chatbots (experiments) and full-sync TenantMembership records.

    OCS tokens are **team-scoped** — a successful ``/api/experiments/`` fetch returns
    only the team the user selected during OAuth consent. Archival is therefore
    restricted to that team's memberships (``archive_team_slug``); memberships from
    other teams the user previously authorized are left untouched.
    """
    base_url = getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")

    team_slug = await _ocs_team_slug(user)
    team_name = (await adetect_team_name_from_oauth(access_token, base_url)) or team_slug

    conn, _ = await TenantConnection.objects.aget_or_create(
        user=user, provider="ocs", credential_type=TenantConnection.OAUTH
    )

    experiments: list[dict] = []
    url: str | None = f"{base_url}/api/experiments/"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code in (401, 403):
                raise OCSAuthError(
                    f"OCS returned {resp.status_code} — access token may have expired"
                )
            resp.raise_for_status()
            payload = resp.json()
            if "results" not in payload:  # shape-drift guard
                raise TenantResolutionError("OCS response missing 'results' key")
            experiments.extend(payload["results"])
            url = payload.get("next")

    fresh = []
    for exp in experiments:
        tenant, _ = await Tenant.objects.aupdate_or_create(
            provider="ocs",
            external_id=str(exp["id"]),
            defaults={"canonical_name": exp.get("name") or str(exp["id"])},
        )
        fresh.append(tenant)

    memberships = await _sync_memberships(
        user,
        conn,
        fresh,
        membership_extra={"team_slug": team_slug, "team_name": team_name},
        archive_team_slug=team_slug,
    )
    logger.info(
        "Resolved %d OCS chatbots for user %s (team %s)", len(memberships), user.email, team_slug
    )
    return memberships


async def _fetch_all_domains(access_token: str) -> list[dict]:
    """Paginate through the CommCare user_domains API, returning the COMPLETE set.

    Tastypie returns a *relative* ``meta.next`` (e.g. ``/api/user_domains/v1/?offset=20``),
    so we resolve it against the base URL. A ``next`` that resolves to a different host
    is refused with a raise rather than silently truncating — a partial list must never
    look "successful," or full-sync would archive every domain past page 1.
    Raises CommCareAuthError on 401/403; TenantResolutionError on shape drift.
    """
    results: list[dict] = []
    url: str | None = COMMCARE_DOMAIN_API
    base_netloc = urlparse(COMMCARE_DOMAIN_API).netloc
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code in (401, 403):
                raise CommCareAuthError(
                    f"CommCare returned {resp.status_code} — access token may have expired"
                )
            resp.raise_for_status()
            data = resp.json()
            if "objects" not in data:  # shape-drift guard
                raise TenantResolutionError("CommCare response missing 'objects' key")
            results.extend(data["objects"])
            next_url = (data.get("meta") or {}).get("next")
            if not next_url:
                url = None
            else:
                resolved = urljoin(COMMCARE_DOMAIN_API, next_url)
                if urlparse(resolved).netloc != base_netloc:
                    raise TenantResolutionError(
                        f"CommCare pagination points off-host, refusing to truncate: {next_url!r}"
                    )
                url = resolved
    return results
