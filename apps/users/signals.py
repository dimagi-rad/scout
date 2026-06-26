"""Signal receivers for social account events and workspace auto-creation."""

import logging

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.users.services.merge import merge_users
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)

logger = logging.getLogger(__name__)


def _trusted_email_providers() -> set[str]:
    """allauth provider ids we trust to have verified the email upstream.

    Derived from ``SOCIALACCOUNT_PROVIDERS[<id>]["VERIFIED_EMAIL"] is True``.
    These are the Dimagi-operated IdPs (CommCare HQ, CommCare Connect, OCS) whose
    ``extract_email_addresses`` returns a verified ``EmailAddress`` on login.
    """
    providers = getattr(settings, "SOCIALACCOUNT_PROVIDERS", {}) or {}
    return {pid for pid, cfg in providers.items() if (cfg or {}).get("VERIFIED_EMAIL") is True}


def _canonical_provably_owns_email(canonical, email: str) -> bool:
    """Whether ``canonical`` has *proven* it owns ``email`` (01#8).

    Auto-merge folds the incoming OAuth identity INTO ``canonical``, so we must
    be sure ``canonical`` is the legitimate owner of the email — otherwise an
    attacker who ``/signup``'d with a victim's email (signup_view bypasses
    allauth, creating no ``EmailAddress``) could absorb the victim's OAuth
    account on the victim's next login (closed by commit 1dc1d58).

    Ownership is proven by EITHER:

    1. a verified allauth ``EmailAddress`` for that email (the OAuth->OAuth case:
       a prior trusted-provider login persisted one), OR
    2. a ``SocialAccount`` on ``canonical`` from a trusted provider whose login
       asserted this email — same upstream-verified signal, robust to the case
       where the verified ``EmailAddress`` row was never persisted/got out of
       sync.

    SEAM (01#8 / #258): a canonical that owns the email ONLY via a password
    ``/signup`` satisfies NEITHER and is (correctly) refused here. Making the
    password->OAuth path auto-link safely needs allauth-side email verification
    at signup — that perimeter is owned by issue #258. See the PR body.
    """
    if EmailAddress.objects.filter(
        user=canonical,
        email__iexact=email,
        verified=True,
    ).exists():
        return True

    trusted = _trusted_email_providers()
    if not trusted:
        return False
    for account in SocialAccount.objects.filter(user=canonical, provider__in=trusted):
        account_email = (account.extra_data or {}).get("email") or ""
        if account_email.strip().lower() == email.strip().lower():
            return True
    return False


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

    # A resolution failure must NOT break login (we can't 500 the OAuth
    # callback), but it must be surfaced loudly: log at ERROR via
    # logger.exception so Sentry pages. Logging at WARNING left the user with
    # zero TenantMembership rows and an empty data-sources page that looked
    # identical to "account has no opportunities", with nobody told (07#6).
    if provider == "commcare_connect":
        try:
            async_to_sync(resolve_connect_opportunities)(sociallogin.user, token.token)
        except Exception:
            logger.exception("Failed to resolve Connect opportunities after OAuth")
    elif provider == "ocs":
        try:
            async_to_sync(resolve_ocs_chatbots)(sociallogin.user, token.token)
        except Exception:
            logger.exception("Failed to resolve OCS chatbots after OAuth")
    elif provider.startswith("commcare"):
        try:
            async_to_sync(resolve_commcare_domains)(sociallogin.user, token.token)
        except Exception:
            logger.exception("Failed to resolve CommCare domains after OAuth")


def reconcile_existing_user_on_login(sender, request, sociallogin, **kwargs):
    """Bridge the gap where allauth's _lookup_by_socialaccount short-circuits.

    When an existing OAuth user logs in and the provider now returns an email
    that the User row doesn't yet have, either backfill it or merge into the
    user that already owns that email.
    """
    new_email = sociallogin.account.extra_data.get("email")
    if not new_email:
        return
    user = sociallogin.user
    if user.pk is None:
        return  # brand-new user; allauth's _lookup_by_email handles it
    if user.email:
        return  # already has an email — nothing to reconcile

    UserModel = get_user_model()
    canonical = UserModel.objects.filter(email__iexact=new_email).exclude(pk=user.pk).first()
    if canonical is None:
        user.email = new_email
        user.save(update_fields=["email"])
        return

    if not _canonical_provably_owns_email(canonical, new_email):
        logger.warning(
            "Refusing auto-merge: canonical user=%s has not proven ownership of %s "
            "(no verified EmailAddress and no trusted-provider SocialAccount)",
            canonical.pk,
            new_email,
        )
        return

    original_pk = user.pk
    try:
        merge_users(canonical=canonical, duplicate=user)
    except Exception:
        logger.exception(
            "Auto-merge failed for user=%s into canonical=%s",
            original_pk,
            canonical.pk,
        )
        return
    sociallogin.user = canonical
    sociallogin.account.user = canonical
    logger.info(
        "auto-merge: user=%s into canonical=%s email=%s provider=%s",
        original_pk,
        canonical.pk,
        new_email,
        sociallogin.account.provider,
    )
