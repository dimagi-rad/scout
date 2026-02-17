"""OAuth token refresh service.

Handles refreshing expired OAuth tokens for CommCare providers.
Called proactively (before token expires) and reactively (after 401).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

# Refresh tokens that expire within this window
REFRESH_BUFFER = timedelta(minutes=5)


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


def refresh_oauth_token(social_token, token_url: str) -> str:
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
        logger.error("Token refresh failed for app %s: %s", social_token.app.client_id, e)
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    data = response.json()
    social_token.token = data["access_token"]
    if data.get("refresh_token"):
        social_token.token_secret = data["refresh_token"]
    if data.get("expires_in"):
        social_token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    social_token.save()

    logger.info("Successfully refreshed OAuth token for app %s", social_token.app.client_id)
    return social_token.token
