"""OAuth token refresh service.

Handles refreshing expired OAuth tokens for the OAuth providers.

Two entry points:

- ``refresh_oauth_token`` (async): the proactive path. The credential resolver
  renews a near-expiry token at task start, and ``providers_view`` renews on poll.
- ``refresh_oauth_token_sync`` (sync): the reactive path. A loader running under
  ``asyncio.to_thread`` calls it on a mid-run 401 to renew a token whose lifetime
  is shorter than the run (CommCare's 15-min OAuth TTL) without bridging back to
  the event loop (arch #252, findings 14#3/14#4).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Refresh tokens that expire within this window
REFRESH_BUFFER = timedelta(minutes=5)


def _ocs_token_url() -> str:
    return f"{settings.OCS_URL.rstrip('/')}/o/token/"


PROVIDER_TOKEN_URLS = {
    "commcare": "https://www.commcarehq.org/oauth/token/",
    "commcare_connect": "https://connect.dimagi.com/o/token/",
}


def get_token_url(provider: str) -> str | None:
    """Return the OAuth token endpoint for a provider, or None if unknown."""
    if provider == "ocs":
        return _ocs_token_url()
    return PROVIDER_TOKEN_URLS.get(provider)


class TokenRefreshError(Exception):
    """Raised when token refresh fails."""


def token_needs_refresh(expires_at: timezone.datetime | None) -> bool:
    """Check if a token needs refreshing based on its expiry time.

    Returns True if the token expires within REFRESH_BUFFER.
    Returns False if expires_at is None (unknown expiry -- assume valid).
    """
    if expires_at is None:
        return False
    return timezone.now() + REFRESH_BUFFER >= expires_at


async def refresh_oauth_token(social_token, token_url: str) -> str:
    """Refresh an OAuth token using the refresh token grant.

    Args:
        social_token: allauth SocialToken instance with token_secret (refresh token)
            and app (SocialApp with client_id and secret).
        token_url: The provider's token endpoint URL.

    Returns:
        The new access token string.

    Raises:
        TokenRefreshError: If the refresh request fails.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": social_token.token_secret,
                    "client_id": social_token.app.client_id,
                    "client_secret": social_token.app.secret,
                },
            )
            response.raise_for_status()
    except Exception as e:
        logger.exception("Token refresh failed for app %s", social_token.app.client_id)
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    data = response.json()
    social_token.token = data["access_token"]
    if data.get("refresh_token"):
        social_token.token_secret = data["refresh_token"]
    if data.get("expires_in"):
        social_token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    await social_token.asave()

    logger.info("Successfully refreshed OAuth token for app %s", social_token.app.client_id)
    return social_token.token


def refresh_oauth_token_sync(social_token, token_url: str) -> str:
    """Blocking OAuth refresh for the materialization worker thread.

    Mirrors ``refresh_oauth_token`` but uses ``requests`` + sync ORM so a loader
    running under ``asyncio.to_thread`` can renew a token mid-run (arch #252,
    finding 14#3). The rotated token is persisted so the next run's proactive
    refresh sees it; a persistence failure is non-fatal — the in-memory token is
    still usable for the remainder of this run.
    """
    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": social_token.token_secret,
                "client_id": social_token.app.client_id,
                "client_secret": social_token.app.secret,
            },
            timeout=30,
        )
        response.raise_for_status()
    except Exception as e:
        logger.exception("Sync token refresh failed for app %s", social_token.app.client_id)
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    data = response.json()
    social_token.token = data["access_token"]
    if data.get("refresh_token"):
        social_token.token_secret = data["refresh_token"]
    if data.get("expires_in"):
        social_token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    try:
        social_token.save(update_fields=["token", "token_secret", "expires_at"])
    except Exception:
        logger.warning(
            "Failed to persist mid-run refreshed token for app %s",
            social_token.app.client_id,
            exc_info=True,
        )
    logger.info("Successfully refreshed OAuth token (sync) for app %s", social_token.app.client_id)
    return social_token.token
