"""Synchronous credential resolution for TenantMembership."""

import logging

logger = logging.getLogger(__name__)


def resolve_credential(membership) -> dict | None:
    """Resolve a credential dict for a TenantMembership, or return None.

    Returns a dict with keys ``type`` (``"api_key"`` or ``"oauth"``) and
    ``value`` (the decrypted key or OAuth token string), or ``None`` if no
    usable credential is found.
    """
    from apps.users.models import TenantCredential

    try:
        cred_obj = TenantCredential.objects.get(tenant_membership=membership)
    except TenantCredential.DoesNotExist:
        return None

    if cred_obj.credential_type == TenantCredential.API_KEY:
        from apps.users.adapters import decrypt_credential

        try:
            decrypted = decrypt_credential(cred_obj.encrypted_credential)
            return {"type": "api_key", "value": decrypted}
        except Exception:
            logger.exception("Failed to decrypt API key for membership %s", membership.id)
            return None

    # OAuth credential
    from allauth.socialaccount.models import SocialToken

    provider = membership.tenant.provider
    if provider == "commcare_connect":
        token_obj = SocialToken.objects.filter(
            account__user=membership.user,
            account__provider__startswith="commcare_connect",
        ).first()
    else:
        token_obj = (
            SocialToken.objects.filter(
                account__user=membership.user,
                account__provider__startswith="commcare",
            )
            .exclude(account__provider__startswith="commcare_connect")
            .first()
        )

    if not token_obj:
        return None
    return {"type": "oauth", "value": token_obj.token}
