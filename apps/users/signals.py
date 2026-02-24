"""Signal receivers for social account events."""

import logging

logger = logging.getLogger(__name__)


def resolve_tenant_on_social_login(request, sociallogin, **kwargs):
    """After CommCare OAuth, resolve domains and create TenantMembership records."""
    provider = sociallogin.account.provider
    if not provider.startswith("commcare") or provider.startswith("commcare_connect"):
        return

    token = sociallogin.token
    if not token or not token.token:
        logger.warning("No access token available after CommCare OAuth for %s", sociallogin.user)
        return

    try:
        from apps.users.services.tenant_resolution import resolve_commcare_domains

        resolve_commcare_domains(sociallogin.user, token.token)
    except Exception:
        logger.warning("Failed to resolve CommCare domains after OAuth", exc_info=True)
