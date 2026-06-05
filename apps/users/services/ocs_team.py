"""Detect an OCS team (slug, name) for a credential, using only verified endpoints.

The only API-key-reachable source of a team's name/slug is ``/api/sessions/``
(each session embeds a ``team`` object). It returns nothing only when the team
has zero sessions, in which case the caller must fall back to a user-supplied
team name.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return getattr(settings, "OCS_URL", "https://www.openchatstudio.com").rstrip("/")


async def _team_from_sessions(headers: dict, base_url: str) -> tuple[str, str] | None:
    """GET /api/sessions/?page_size=1 -> (slug, name), or None when no sessions."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base_url}/api/sessions/", headers=headers, params={"page_size": 1}
            )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results") or []
        if not results:
            return None
        team = results[0].get("team") or {}
        slug, name = team.get("slug"), team.get("name")
        if slug and name:
            return str(slug), str(name)
    except Exception:
        logger.warning("OCS team detection via sessions failed", exc_info=True)
    return None


async def adetect_team_from_api_key(
    api_key: str, base_url: str | None = None
) -> tuple[str, str] | None:
    """Return (team_slug, team_name) for an OCS API key, or None if undetectable."""
    return await _team_from_sessions({"X-api-key": api_key}, base_url or _base_url())


async def adetect_team_name_from_oauth(
    access_token: str, base_url: str | None = None
) -> str | None:
    """Best-effort friendly team name for an OAuth token (slug comes from the OIDC claim)."""
    res = await _team_from_sessions(
        {"Authorization": f"Bearer {access_token}"}, base_url or _base_url()
    )
    return res[1] if res else None
