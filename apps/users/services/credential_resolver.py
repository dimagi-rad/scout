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

_PROVIDER_LABELS = {
    "commcare": "CommCare",
    "commcare_connect": "CommCare Connect",
    "ocs": "Open Chat Studio",
}


def _reauth_message(provider: str) -> str:
    """User-facing 'reconnect your account' guidance for an expired token."""
    label = _PROVIDER_LABELS.get(provider)
    who = f"Your {label} sign-in" if label else "Your sign-in"
    what = f"reconnect your {label} account" if label else "reconnect your account"
    return (
        f"{who} has expired or been revoked and could not be renewed "
        f"automatically. Please {what} and retry."
    )


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


async def aget_fresh_access_token(user, provider: str) -> str | None:
    """Return a usable OAuth access token for *user*/*provider*, refreshing it if
    near expiry, or None if the user has no usable token.

    Unlike a raw token read, this refreshes an expired token — important for
    server-side refresh on behalf of a user who hasn't logged in recently (their
    access token is likely stale). Returns the same token value the materializer
    would use for this user+provider.
    """
    token_obj = await _social_token_qs(user, provider).select_related("account", "app").afirst()
    if token_obj is None:
        return None
    try:
        cred = await _aresolve_oauth_credential(token_obj, provider)
    except CredentialResolutionError:
        # A refresh failure means no usable token. Callers of this helper only
        # want a live token (e.g. share-time tenant-access refresh) and treat
        # None as "skip"; surface None rather than raising into them.
        return None
    return cred["value"]


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
    """Build an OAuth credential dict, refreshing the token if near expiry.

    Fails closed (raises ``CredentialResolutionError`` with ``AUTH_TOKEN_EXPIRED``)
    when the token is at/near expiry and cannot be renewed. Serving a known-stale
    token only provisions a schema and burns the discover phase before the first
    authenticated request 401s, and no 401 downstream maps to actionable
    re-authentication guidance — so we surface "reconnect your account" up front
    instead of a doomed run (arch #252, finding 14#4).
    """
    token_url = get_token_url(provider)
    can_refresh = bool(token_url and token_obj.token_secret and token_obj.app)

    if token_needs_refresh(token_obj.expires_at):
        if not can_refresh:
            raise CredentialResolutionError(AUTH_TOKEN_EXPIRED, _reauth_message(provider))
        try:
            return {"type": "oauth", "value": await refresh_oauth_token(token_obj, token_url)}
        except TokenRefreshError as e:
            logger.warning("Token refresh failed for provider %s; failing closed", provider)
            raise CredentialResolutionError(AUTH_TOKEN_EXPIRED, _reauth_message(provider)) from e

    return {"type": "oauth", "value": token_obj.token}
