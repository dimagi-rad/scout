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


def resolve_credential(membership, team_id: str | None = None) -> dict | None:
    """Resolve a credential dict for a TenantMembership, optionally filtered by team_id.

    Args:
        membership: The TenantMembership to resolve
        team_id: Optional team_id for multi-credential lookups. If specified and not
                 found, returns None (fail closed). If not provided, prefers OAuth
                 credential; returns None if no OAuth credential exists.

    Returns a dict with keys ``type`` (``"api_key"`` or ``"oauth"``) and
    ``value`` (the decrypted key or OAuth token string), or ``None`` if no
    usable credential is found.
    """
    try:
        if team_id is not None:
            # Explicit team_id requested: must find exact match (API_KEY with this team_id)
            # If not found, return None (don't use wrong team's secret)
            cred_obj = TenantCredential.objects.get(
                tenant_membership=membership,
                credential_type=TenantCredential.API_KEY,
                team_id=team_id,
            )
        else:
            # No team_id specified: prefer OAuth (team_id="", credential_type=OAUTH)
            try:
                cred_obj = TenantCredential.objects.get(
                    tenant_membership=membership,
                    credential_type=TenantCredential.OAUTH,
                    team_id="",
                )
            except TenantCredential.DoesNotExist:
                # No OAuth found, fall back to first API_KEY credential
                cred_obj = TenantCredential.objects.filter(
                    tenant_membership=membership,
                    credential_type=TenantCredential.API_KEY,
                ).first()
    except TenantCredential.DoesNotExist:
        if team_id is not None:
            # Requested team_id was not found - return None (fail closed)
            return None
        # Should not reach here (handled in the else clause above)
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


async def aresolve_credential(membership, team_id: str | None = None) -> dict | None:
    """Async version of :func:`resolve_credential` with token refresh.

    Args:
        membership: The TenantMembership to resolve
        team_id: Optional team_id for multi-credential lookups. If not provided,
                 prefers OAuth credential (team_id=NULL, credential_type=OAUTH),
                 then falls back to first available API key credential.

    Like the sync variant, returns a ``{"type": ..., "value": ...}`` dict or
    ``None``.  For OAuth tokens, attempts a refresh when the token is near
    expiry.
    """
    cred_obj = None

    if team_id is not None:
        # Look up API key credential with specific team_id
        # If requested team_id is not found, return None (fail closed, don't use wrong team)
        try:
            cred_obj = await TenantCredential.objects.select_related(
                "tenant_membership"
            ).aget(
                tenant_membership=membership,
                credential_type=TenantCredential.API_KEY,
                team_id=team_id,
            )
        except TenantCredential.DoesNotExist:
            return None
    else:
        # team_id is None: prefer OAuth credential (team_id="", credential_type=OAUTH)
        cred_obj = await TenantCredential.objects.filter(
            tenant_membership=membership,
            credential_type=TenantCredential.OAUTH,
            team_id="",
        ).afirst()
        if not cred_obj:
            # No OAuth found, fall back to first available API_KEY credential
            # This allows single-credential workflows (no OAuth) to still work
            cred_obj = await TenantCredential.objects.filter(
                tenant_membership=membership,
                credential_type=TenantCredential.API_KEY,
            ).afirst()

    if not cred_obj:
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
