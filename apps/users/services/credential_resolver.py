"""Credential resolution for TenantMembership."""

from __future__ import annotations

import logging
from collections.abc import Callable

from allauth.socialaccount.models import SocialToken

from apps.users.adapters import decrypt_credential
from apps.users.models import TenantConnection
from apps.users.services.oauth_scope import is_active_identity, provider_accounts
from apps.users.services.token_refresh import (
    TokenRefreshError,
    get_token_url,
    refresh_oauth_token,
    refresh_oauth_token_sync,
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
    """Use the shared provider mapping, ordered newest identity first."""
    qs = SocialToken.objects.filter(account__in=provider_accounts(user, provider))
    return qs.order_by("-account__date_joined", "-account__id")


async def aiter_social_tokens(user, provider: str) -> list[SocialToken]:
    """Active SocialTokens for *user* and *provider*, newest identity first.

    Existing scope bindings exclude superseded or unvalidated identities.
    Callers that resolve or refresh
    upstream access must iterate all of them: picking one would silently ignore
    the teams the user is not "currently" signed in to, which is exactly the
    lockout #156 exists to remove.
    """
    bindings = {
        (conn.provider, conn.scope_key): conn.social_account_id
        async for conn in TenantConnection.objects.filter(
            user=user, provider=provider, credential_type=TenantConnection.OAUTH
        )
    }
    return [
        token
        async for token in _social_token_qs(user, provider).select_related("account", "app")
        if is_active_identity(token.account, bindings, provider=provider)
    ]


async def aget_connection_token(conn) -> SocialToken | None:
    """The SocialToken belonging to *conn*'s own allauth identity.

    Resolving through ``conn.social_account`` is what makes N tokens per provider
    safe: the connection names the identity whose token it is, so no ordering
    heuristic decides which team Scout authenticates as. Connections written
    before the scope backfill have no linked account and fall back to the
    provider-wide (now ordered) read.
    """
    if conn.social_account_id:
        return (
            await SocialToken.objects.filter(account_id=conn.social_account_id)
            .select_related("account", "app")
            .afirst()
        )
    # user_id, not user: callers select_related("connection") but not its user, so
    # touching conn.user here would be a sync FK fetch inside an async view.
    return (
        await _social_token_qs(conn.user_id, conn.provider)
        .select_related("account", "app")
        .afirst()
    )


async def aiter_fresh_access_tokens(user, provider: str) -> list[tuple]:
    """Every usable ``(identity, access token)`` pair for *user*/*provider*.

    For callers checking upstream access, one representative token answers only
    about its team and silently ignores the rest.
    Identities whose token cannot be renewed are dropped rather than raised on —
    a dead credential for one team must not abort the others.
    """
    pairs = []
    for token_obj in await aiter_social_tokens(user, provider):
        try:
            cred = await _aresolve_oauth_credential(token_obj, provider)
        except CredentialResolutionError:
            continue
        pairs.append((token_obj.account, cred["value"]))
    return pairs


def _oauth_team_mismatch(membership, conn, token_obj) -> bool:
    """True when the chatbot's team is known and this connection is scoped elsewhere.

    The chatbot's team lives on the membership (``team_slug``). The team the
    connection speaks for is ``conn.scope_key``, recorded when the credential was
    authorised; the OIDC ``team`` claim on the token's own account is the fallback
    for connections predating that field. When they differ we must not use this
    token — fail closed.

    Still needed after multi-token OAuth: memberships that a single shared
    connection accumulated across two teams keep pointing at it until the user
    re-authorises the second team, and serving them team A's token would be the
    cross-team read this check was written to stop.
    """
    if not membership.team_slug:
        return False
    current = conn.scope_key or (getattr(token_obj.account, "extra_data", None) or {}).get("team")
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

    token_obj = await aget_connection_token(conn)
    if not token_obj:
        return None
    if _oauth_team_mismatch(membership, conn, token_obj):
        # This connection's credential belongs to a different team than this
        # chatbot. Fail closed (never serve another team's token), but surface a
        # distinct, actionable error so the user is told to connect that team —
        # not the generic "No credential configured" (arch #245 finding 07#3).
        raise CredentialResolutionError(
            AUTH_TOKEN_EXPIRED,
            "Your sign-in is scoped to a different team than this chatbot's "
            f"team ({membership.team_slug}). Please connect team "
            f"'{membership.team_slug}' to materialize it.",
        )

    return await _aresolve_oauth_credential(token_obj, conn.provider)


async def aconnection_status(conn) -> str:
    """Read connection health without rotating credentials or doing network I/O.

    providers_view owns the settings page's proactive refresh. The client reads
    this status after that request completes so it sees the updated expiry.
    """
    if conn.credential_type != TenantConnection.OAUTH:
        return "unknown"
    token_obj = await aget_connection_token(conn)
    if token_obj is None:
        return "expired"
    token_url = get_token_url(conn.provider)
    can_refresh = bool(token_url and token_obj.token_secret and token_obj.app)
    if not token_needs_refresh(token_obj.expires_at, can_refresh=can_refresh):
        if token_obj.expires_at is None and token_url is not None:
            return "expired"
        return "connected"
    return "expired"


def _make_token_refresher(token_obj, token_url: str) -> Callable[[], str]:
    """Return a sync callable a loader invokes on a mid-run 401 to mint a fresh
    access token (arch #252, finding 14#3).

    It runs on the materialization worker thread (``asyncio.to_thread``), so it
    uses the blocking refresh + sync-ORM persistence. It closes over the same
    ``token_obj`` the proactive refresh mutated, so a second mid-run refresh
    reuses the rotated refresh token.
    """

    def _refresh() -> str:
        return refresh_oauth_token_sync(token_obj, token_url)

    return _refresh


async def _aresolve_oauth_credential(token_obj, provider: str) -> dict:
    """Build an OAuth credential dict, refreshing the token if near expiry.

    Fails closed (raises ``CredentialResolutionError`` with ``AUTH_TOKEN_EXPIRED``)
    when the token is at/near expiry and cannot be renewed. Serving a known-stale
    token only provisions a schema and burns the discover phase before the first
    authenticated request 401s, and no 401 downstream maps to actionable
    re-authentication guidance — so we surface "reconnect your account" up front
    instead of a doomed run (arch #252, finding 14#4).

    When a refresh is possible the credential carries a ``refresh`` callable so
    loaders can renew the token mid-run and survive a token whose lifetime is
    shorter than the run — CommCare's 15-min OAuth TTL (finding 14#3).
    """
    token_url = get_token_url(provider)
    can_refresh = bool(token_url and token_obj.token_secret and token_obj.app)

    token_value = token_obj.token
    # can_refresh: an unknown expiry is only actionable when there is a refresh
    # token to test it with, so it does not condemn a credential we cannot check
    # (#373). With one available we now attempt the refresh, which is what turns
    # a revoked-but-unexpired token into an up-front "reconnect" rather than a
    # provisioned schema and a doomed run.
    if token_needs_refresh(token_obj.expires_at, can_refresh=can_refresh):
        if not can_refresh:
            raise CredentialResolutionError(AUTH_TOKEN_EXPIRED, _reauth_message(provider))
        try:
            token_value = await refresh_oauth_token(token_obj, token_url)
        except TokenRefreshError as e:
            logger.warning("Token refresh failed for provider %s; failing closed", provider)
            raise CredentialResolutionError(AUTH_TOKEN_EXPIRED, _reauth_message(provider)) from e

    cred: dict = {"type": "oauth", "value": token_value}
    if can_refresh:
        cred["refresh"] = _make_token_refresher(token_obj, token_url)
    return cred
