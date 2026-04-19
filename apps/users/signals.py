"""Signal receivers for social account events and workspace auto-creation."""

import logging

from asgiref.sync import async_to_sync
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)

logger = logging.getLogger(__name__)


@receiver(post_save, sender="users.TenantMembership")
def auto_create_workspace_on_membership(sender, instance, created, **kwargs):
    """Auto-create a workspace for newly created TenantMembership records."""
    if not created:
        return
    from apps.workspaces.models import (
        Workspace,
        WorkspaceMembership,
        WorkspaceRole,
        WorkspaceTenant,
    )

    # Idempotent: skip if an auto-created workspace for this user+tenant already exists
    existing = Workspace.objects.filter(
        is_auto_created=True,
        memberships__user=instance.user,
        workspace_tenants__tenant=instance.tenant,
    ).first()
    if existing:
        return

    workspace = Workspace.objects.create(
        name=instance.tenant.canonical_name,
        is_auto_created=True,
        created_by=instance.user,
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=instance.tenant)
    WorkspaceMembership.objects.create(
        workspace=workspace,
        user=instance.user,
        role=WorkspaceRole.MANAGE,
    )


def resolve_tenant_on_social_login(request, sociallogin, **kwargs):
    """After CommCare/Connect OAuth, resolve tenants and create TenantMembership records."""
    provider = sociallogin.account.provider

    token = sociallogin.token
    if not token or not token.token:
        logger.warning("No access token available after OAuth for %s", sociallogin.user)
        return

    if provider == "commcare_connect":
        try:
            async_to_sync(resolve_connect_opportunities)(sociallogin.user, token.token)
        except Exception:
            logger.warning("Failed to resolve Connect opportunities after OAuth", exc_info=True)
    elif provider == "ocs":
        try:
            async_to_sync(resolve_ocs_chatbots)(sociallogin.user, token.token)
        except Exception:
            logger.warning("Failed to resolve OCS chatbots after OAuth", exc_info=True)
    elif provider.startswith("commcare"):
        try:
            async_to_sync(resolve_commcare_domains)(sociallogin.user, token.token)
        except Exception:
            logger.warning("Failed to resolve CommCare domains after OAuth", exc_info=True)
