"""Credential resolution for TenantMembership."""

from __future__ import annotations

import logging

from allauth.socialaccount.models import SocialToken

from apps.users.adapters import decrypt_credential
from apps.users.models import TenantCredential
from apps.users.services.token_refresh import (
    TokenRefreshError,
    get_token_url,
    refresh_oauth_token,
    token_needs_refresh,
)

logger = logging.getLogger(__name__)


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


def get_social_token(user, provider: str) -> SocialToken | None:
    """Return the SocialToken for *user* and *provider*, or None."""
    return _social_token_qs(user, provider).first()


async def aget_social_token(user, provider: str) -> SocialToken | None:
    """Async version of :func:`get_social_token`."""
    return await _social_token_qs(user, provider).afirst()


def resolve_credential(membership) -> dict | None:
    """Resolve a credential dict for a TenantMembership, or return None.

    Returns a dict with keys ``type`` (``"api_key"`` or ``"oauth"``) and
    ``value`` (the decrypted key or OAuth token string), or ``None`` if no
    usable credential is found.
    """
    try:
        cred_obj = TenantCredential.objects.get(tenant_membership=membership)
    except TenantCredential.DoesNotExist:
        return None

    if cred_obj.credential_type == TenantCredential.API_KEY:
        try:
            decrypted = decrypt_credential(cred_obj.encrypted_credential)
            return {"type": "api_key", "value": decrypted}
        except Exception:
            logger.exception("Failed to decrypt API key for membership %s", membership.id)
            return None

    token_obj = get_social_token(membership.user, membership.tenant.provider)
    if not token_obj:
        return None
    return {"type": "oauth", "value": token_obj.token}


async def aresolve_credential(membership) -> dict | None:
    """Async version of :func:`resolve_credential` with token refresh.

    Like the sync variant, returns a ``{"type": ..., "value": ...}`` dict or
    ``None``.  For OAuth tokens, attempts a refresh when the token is near
    expiry.
    """
    try:
        cred_obj = await TenantCredential.objects.select_related("tenant_membership").aget(
            tenant_membership=membership
        )
    except TenantCredential.DoesNotExist:
        return None

    if cred_obj.credential_type == TenantCredential.API_KEY:
        try:
            decrypted = decrypt_credential(cred_obj.encrypted_credential)
            return {"type": "api_key", "value": decrypted}
        except Exception:
            logger.exception("Failed to decrypt API key for membership %s", membership.id)
            return None

    provider = membership.tenant.provider
    token_obj = await _social_token_qs(membership.user, provider).select_related("app").afirst()
    if not token_obj:
        return None

    return await _aresolve_oauth_credential(token_obj, provider)


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
