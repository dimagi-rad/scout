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

import hashlib
import json
import logging
from datetime import timedelta

import httpx
import requests
from allauth.socialaccount.models import SocialToken
from django.conf import settings
from django.utils import timezone

from apps.users.models import TenantConnection
from apps.users.services.oauth_scope import canonical_provider

logger = logging.getLogger(__name__)

# Refresh tokens that expire within this window
REFRESH_BUFFER = timedelta(minutes=5)


def _ocs_token_url() -> str:
    return f"{settings.OCS_URL.rstrip('/')}/o/token/"


def _connect_token_url() -> str:
    return f"{settings.CONNECT_API_URL.rstrip('/')}/o/token/"


PROVIDER_TOKEN_URLS = {
    "commcare": "https://www.commcarehq.org/oauth/token/",
}


def get_token_url(provider: str) -> str | None:
    """Return the OAuth token endpoint for a provider, or None if unknown."""
    provider = canonical_provider(provider)
    if provider == "ocs":
        return _ocs_token_url()
    if provider == "commcare_connect":
        return _connect_token_url()
    return PROVIDER_TOKEN_URLS.get(provider)


class TokenRefreshError(Exception):
    """Raised when token refresh fails."""


def token_needs_refresh(expires_at: timezone.datetime | None, *, can_refresh: bool = True) -> bool:
    """Check if a token needs refreshing based on its expiry time.

    Returns True if the token expires within REFRESH_BUFFER.

    An **unknown** expiry (``None``) used to return False — "assume valid" — which
    fails open. ``expires_at`` is the only health signal Scout has, so a revoked
    token with no recorded expiry was never proactively refreshed, was handed
    straight to the loaders, and reported "connected" in the UI right up until it
    401'd (#373). Unknown expiry now counts as needing a refresh, which is the one
    action that can actually establish whether the credential is still alive.

    ``can_refresh`` guards that: with no refresh token there is nothing to
    attempt, and returning True would only condemn a credential that may well be
    working. So unknown-expiry-and-unrefreshable keeps the old answer, and its
    honesty problem is fixed where it is visible instead — ``providers_view``
    reports such a token as needing reconnection rather than asserting it is fine.
    """
    if expires_at is None:
        return can_refresh
    return timezone.now() + REFRESH_BUFFER >= expires_at


def credential_fingerprint(token) -> str:
    value = json.dumps([token.token, token.token_secret, token.app_id])
    return hashlib.sha256(value.encode()).hexdigest()


def token_health(token, provider, *, refresh_failed=False) -> str:
    """User-action status, independent of the proactive refresh buffer."""
    if refresh_failed:
        return "expired"
    token_url = get_token_url(provider)
    if token_url and token.token_secret and token.app:
        return "connected"
    if token.expires_at is not None:
        return "connected" if token.expires_at > timezone.now() else "expired"
    return "expired" if token_url else "connected"


def _token_connections(token):
    # A late failure from a replaced token must not erase the replacement's outcome.
    current = SocialToken.objects.filter(
        pk=token.pk, token=token.token, token_secret=token.token_secret, app_id=token.app_id
    ).values("account_id")
    return TenantConnection.objects.filter(
        social_account_id__in=current, credential_type=TenantConnection.OAUTH
    )


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
    fingerprint = credential_fingerprint(social_token)
    if social_token.app is None:
        raise TokenRefreshError("OAuth application is missing; reconnect this account.")
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
    except httpx.HTTPStatusError as e:
        # A 4xx (typically 400 invalid_grant on a dead refresh token) is an
        # expected outcome, not a bug -- log at WARNING with the body so we can
        # tell invalid_grant (dead token) from invalid_client (bad secret).
        if 400 <= e.response.status_code < 500:
            logger.warning(
                "Token refresh rejected for app %s: HTTP %s %s",
                social_token.app.client_id,
                e.response.status_code,
                e.response.text,
            )
        else:
            logger.exception("Token refresh failed for app %s", social_token.app.client_id)
        await _token_connections(social_token).aupdate(
            oauth_refresh_failure_fingerprint=fingerprint
        )
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e
    except Exception as e:
        logger.exception("Token refresh failed for app %s", social_token.app.client_id)
        await _token_connections(social_token).aupdate(
            oauth_refresh_failure_fingerprint=fingerprint
        )
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    data = response.json()
    social_token.token = data["access_token"]
    if data.get("refresh_token"):
        social_token.token_secret = data["refresh_token"]
    if data.get("expires_in"):
        social_token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    await social_token.asave()
    await (
        _token_connections(social_token)
        .filter(oauth_refresh_failure_fingerprint=fingerprint)
        .aupdate(oauth_refresh_failure_fingerprint="")
    )

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
    fingerprint = credential_fingerprint(social_token)
    if social_token.app is None:
        raise TokenRefreshError("OAuth application is missing; reconnect this account.")
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
    except requests.HTTPError as e:
        # Mirrors the async twin above: a 4xx (typically 400 invalid_grant on a
        # dead refresh token) is an expected outcome, not a bug -- log at WARNING
        # with the body so we can tell invalid_grant (dead token) from
        # invalid_client (bad secret). The sync path never got this treatment,
        # so every routine dead-token refresh raised a Sentry event (#373).
        status = e.response.status_code if e.response is not None else None
        if status is not None and 400 <= status < 500:
            logger.warning(
                "Sync token refresh rejected for app %s: HTTP %s %s",
                social_token.app.client_id,
                status,
                e.response.text,
            )
        else:
            logger.exception("Sync token refresh failed for app %s", social_token.app.client_id)
        _token_connections(social_token).update(oauth_refresh_failure_fingerprint=fingerprint)
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e
    except Exception as e:
        logger.exception("Sync token refresh failed for app %s", social_token.app.client_id)
        _token_connections(social_token).update(oauth_refresh_failure_fingerprint=fingerprint)
        raise TokenRefreshError(f"Failed to refresh OAuth token: {e}") from e

    data = response.json()
    social_token.token = data["access_token"]
    if data.get("refresh_token"):
        social_token.token_secret = data["refresh_token"]
    if data.get("expires_in"):
        social_token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    try:
        social_token.save(update_fields=["token", "token_secret", "expires_at"])
        _token_connections(social_token).filter(
            oauth_refresh_failure_fingerprint=fingerprint
        ).update(oauth_refresh_failure_fingerprint="")
    except Exception:
        logger.warning(
            "Failed to persist mid-run refreshed token for app %s",
            social_token.app.client_id,
            exc_info=True,
        )
    logger.info("Successfully refreshed OAuth token (sync) for app %s", social_token.app.client_id)
    return social_token.token
