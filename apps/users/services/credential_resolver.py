"""Credential resolution for TenantMembership."""

from __future__ import annotations

import logging

from allauth.socialaccount.models import SocialToken

from apps.users.adapters import decrypt_credential
from apps.users.models import TenantConnection
from apps.users.services.token_refresh import (
    TokenRefreshError,
    get_token_url,
    refresh_oauth_token,
    token_needs_refresh,
)
from mcp_server.envelope import AUTH_TOKEN_EXPIRED

logger = logging.getLogger(__name__)


class CredentialResolutionError(Exception):
    """Raised when a credential exists but cannot be used for a *known,
    actionable* reason (e.g. the user's OAuth token is now scoped to a
    different team than the chatbot they're materializing).

    Distinct from ``aresolve_credential`` returning ``None`` (which means no
    usable credential could be found for an opaque reason — missing connection,
    decrypt failure, etc.). Callers should surface ``message`` to the user and
    may key UI/remediation off ``code`` (an ``mcp_server.envelope`` error code).
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _social_token_qs(user, provider: str):
    """Return a SocialToken queryset filtered by provider-prefix rules.

    - ``"commcare_connect"`` matches tokens whose provider starts with
      ``"commcare_connect"``.
    - ``"ocs"`` matches tokens whose provider equals ``"ocs"``.
    - Any other provider matches tokens starting with ``"commcare"`` but
      excludes ``"commcare_connect"``.
    """
    if provider == "commcare_connect":
        return SocialToken.objects.filter(
            account__user=user,
            account__provider__startswith="commcare_connect",
        )
    if provider == "ocs":
        return SocialToken.objects.filter(
            account__user=user,
            account__provider="ocs",
        )

    return SocialToken.objects.filter(
        account__user=user,
        account__provider__startswith="commcare",
    ).exclude(account__provider__startswith="commcare_connect")


async def aget_social_token(user, provider: str) -> SocialToken | None:
    """Return the SocialToken for *user* and *provider*, or None."""
    return await _social_token_qs(user, provider).afirst()


def _oauth_team_mismatch(membership, token_obj) -> bool:
    """True when the chatbot's team is known and the live OAuth token is scoped elsewhere.

    The chatbot's team lives on the membership (``team_slug``); the team the
    current OAuth token is scoped to is the OIDC ``team`` claim stored in the
    SocialAccount's ``extra_data``. When they differ we must not use this token
    (it has moved to another OCS team) — fail closed.
    """
    if not membership.team_slug:
        return False
    current = (getattr(token_obj.account, "extra_data", None) or {}).get("team")
    return bool(current) and current != membership.team_slug


async def aresolve_credential(membership) -> dict | None:
    """Resolve a credential dict for a TenantMembership, or return None.

    Resolution is driven by ``membership.connection`` (callers must
    ``select_related("connection", "user")``). Returns a dict with keys ``type``
    (``"api_key"`` or ``"oauth"``) and ``value`` (the decrypted key or OAuth token
    string), or ``None`` if no usable credential is found. For OAuth tokens,
    attempts a refresh when the token is near expiry.
    """
    conn = membership.connection
    if conn is None:
        return None

    if conn.credential_type == TenantConnection.API_KEY:
        try:
            decrypted = decrypt_credential(conn.encrypted_credential)
            return {"type": "api_key", "value": decrypted}
        except Exception:
            logger.exception("Failed to decrypt API key for membership %s", membership.id)
            return None

    token_obj = (
        await _social_token_qs(membership.user, conn.provider)
        .select_related("account", "app")
        .afirst()
    )
    if not token_obj:
        return None
    if _oauth_team_mismatch(membership, token_obj):
        # The user is signed in to a different team than this chatbot. Fail
        # closed (never serve another team's token), but surface a distinct,
        # actionable error so the user is told to re-connect — not the generic
        # "No credential configured" (arch #245 finding 07#3).
        raise CredentialResolutionError(
            AUTH_TOKEN_EXPIRED,
            "Your sign-in is scoped to a different team than this chatbot's "
            f"team ({membership.team_slug}). Please re-connect to team "
            f"'{membership.team_slug}' to materialize it.",
        )

    return await _aresolve_oauth_credential(token_obj, conn.provider)


async def _aresolve_oauth_credential(token_obj, provider: str) -> dict:
    """Build an OAuth credential dict, refreshing the token if near expiry."""
    token_value = token_obj.token

    if token_needs_refresh(token_obj.expires_at):
        token_url = get_token_url(provider)
        if token_url and token_obj.token_secret:
            try:
                token_value = await refresh_oauth_token(token_obj, token_url)
            except TokenRefreshError:
                logger.warning(
                    "Token refresh failed for provider %s, using existing token", provider
                )

    return {"type": "oauth", "value": token_value}
