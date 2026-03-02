"""Signal receivers for social account events."""

import logging

logger = logging.getLogger(__name__)


def resolve_tenant_on_social_login(request, sociallogin, **kwargs):
    """After CommCare/Connect OAuth, resolve tenants and create TenantMembership records."""
    provider = sociallogin.account.provider

    token = sociallogin.token
    if not token or not token.token:
        logger.warning("No access token available after OAuth for %s", sociallogin.user)
        return

    if provider == "commcare_connect":
        try:
            from apps.users.services.tenant_resolution import resolve_connect_opportunities

            resolve_connect_opportunities(sociallogin.user, token.token)
        except Exception:
            logger.warning("Failed to resolve Connect opportunities after OAuth", exc_info=True)
    elif provider.startswith("commcare"):
        try:
            from apps.users.services.tenant_resolution import resolve_commcare_domains

            resolve_commcare_domains(sociallogin.user, token.token)
        except Exception:
            logger.warning("Failed to resolve CommCare domains after OAuth", exc_info=True)
