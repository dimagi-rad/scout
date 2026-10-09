"""Signal receivers for social account events and workspace auto-creation."""

import logging

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.common.errors import OCSAuthError
from apps.users.services import ocs_access_notice
from apps.users.services.email_proof import proven_emails, verified_social_email
from apps.users.services.merge import merge_users
from apps.users.services.oauth_scope import canonical_provider
from apps.users.services.onboarding_cache import me_onboarding_cache_key
from apps.users.services.tenant_resolution import (
    resolve_commcare_domains,
    resolve_connect_opportunities,
    resolve_ocs_chatbots,
)
from apps.workspaces.models import (
    LIVE_INVITE_STATUSES,
    Workspace,
    WorkspaceInvite,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.invite_notifications import (
    notify_awaiting_access,
    notify_invite_accepted,
)
from apps.workspaces.services.member_coverage import accept_invite_if_covered

logger = logging.getLogger(__name__)


def _canonical_provably_owns_email(canonical, email: str) -> bool:
    """Whether ``canonical`` has *proven* it owns ``email`` (01#8).

    Auto-merge folds the incoming OAuth identity INTO ``canonical``, so we must
    be sure ``canonical`` is the legitimate owner of the email — otherwise a
    password account holding a victim's email (admin-created, or registered
    through the since-removed ``/api/auth/signup/``, neither of which creates an
    ``EmailAddress``) could absorb the victim's OAuth account on the victim's
    next login (closed by commit 1dc1d58).

    Ownership is proven by a verified allauth ``EmailAddress`` or by a
    ``SocialAccount`` whose provider vouched for the email (``proven_emails``).

    SEAM (01#8 / #258): a canonical that owns the email ONLY via a password
    account satisfies neither and is (correctly) refused here. Making the
    password->OAuth path auto-link safely needs email verification for password
    accounts — that perimeter is owned by issue #258.
    """
    return email.strip().lower() in proven_emails(canonical)


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def remember_was_active(sender, instance, **kwargs):
    instance._was_active = (
        sender.objects.filter(pk=instance.pk).values_list("is_active", flat=True).first()
        if instance.pk
        else None
    )


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def end_sessions_on_deactivation(sender, instance, created, **kwargs):
    """Drop all DB sessions of a user when a save flips them from active to inactive (#385).

    Sessions are keyed by opaque ids, so we must decode each live one to find the owner.
    QuerySet.update() bypasses signals; deactivate via save().
    """
    if created or instance.is_active or not getattr(instance, "_was_active", False):
        return
    user_pk = str(instance.pk)
    stale = [
        s.pk
        for s in Session.objects.filter(expire_date__gt=timezone.now())
        if s.get_decoded().get(SESSION_KEY) == user_pk
    ]
    Session.objects.filter(pk__in=stale).delete()


@receiver(post_save, sender="users.TenantMembership")
def auto_create_workspace_on_membership(sender, instance, created, **kwargs):
    """Auto-create a workspace for newly created TenantMembership records."""
    if not created:
        return
    # Idempotent: skip if an auto-created workspace for this user+tenant already exists
    existing = Workspace.objects.filter(
        is_auto_created=True,
        memberships__user=instance.user,
        workspace_tenants__tenant=instance.tenant,
    ).first()
    if existing:
        return

    # Atomic so a failure cannot leave a tenantless workspace that
    # load_workspace_context rejects.
    with transaction.atomic():
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


def resolve_existing_tenants_on_social_login(request, sociallogin, **kwargs):
    # allauth's social_account_updated fires before _store_token; pre_social_login is after it.
    if sociallogin.account.pk and sociallogin.is_existing:
        resolve_tenant_on_social_login(request, sociallogin)


def resolve_tenant_on_social_signup(request, user, sociallogin=None, **kwargs):
    # A new user's signup sends neither social_account_added (connect only) nor an
    # existing-account pre_social_login, so without this it resolves nothing until the next login.
    if sociallogin is not None:
        resolve_tenant_on_social_login(request, sociallogin)


def resolve_tenant_on_social_login(request, sociallogin, **kwargs):
    """After CommCare/Connect/OCS OAuth, resolve tenants and create TenantMembership records.

    ``sociallogin.account`` is threaded through so the resolver attributes the
    fetch to *this* identity. It matters because an OCS token is team-scoped and a
    user may hold several. New connects resolve on ``social_account_added`` and new
    users on ``user_signed_up``; existing identities resolve on ``pre_social_login``,
    after allauth stores the refreshed token so the credential observation can be
    checked safely.
    """
    provider = canonical_provider(sociallogin.account.provider)

    token = sociallogin.token
    if not token or not token.token:
        logger.warning("No access token available after OAuth for %s", sociallogin.user)
    # A resolution failure must NOT break login (we can't 500 the OAuth
    # callback), but it must be surfaced loudly: log at ERROR via
    # logger.exception so Sentry pages. Logging at WARNING left the user with
    # zero TenantMembership rows and an empty data-sources page that looked
    # identical to "account has no opportunities", with nobody told (07#6).
    # The one exception is an OCS 403, which the user is told about instead.
    elif provider == "commcare_connect":
        try:
            # allauth signal receivers are sync.
            async_to_sync(resolve_connect_opportunities)(
                sociallogin.user, token.token, social_account=sociallogin.account
            )
        except Exception:
            logger.exception("Failed to resolve Connect opportunities after OAuth")
    elif provider == "ocs":
        outcome = None
        try:
            # allauth signal receivers are sync.
            async_to_sync(resolve_ocs_chatbots)(
                sociallogin.user, token.token, social_account=sociallogin.account
            )
            outcome = "resolved"
        except OCSAuthError as error:
            if error.status_code != 403:
                logger.exception("Failed to resolve OCS chatbots after OAuth")
            else:
                # Usually the user's team permission, which the banner explains, so it no
                # longer pages (SCOUT-DJANGO-3E). Trade-off: a Scout-side cause that 403s
                # every OCS login (e.g. a scope OCS stops accepting) now surfaces only as
                # user reports and these warnings.
                logger.warning("OCS refused the chatbot list after OAuth", exc_info=True)
                outcome = "refused"
        except Exception:
            logger.exception("Failed to resolve OCS chatbots after OAuth")
        try:
            if outcome == "refused":
                ocs_access_notice.record_refusal(request, sociallogin.user, sociallogin.account)
            elif outcome == "resolved":
                ocs_access_notice.clear_refusal(request, sociallogin.user, sociallogin.account)
        except Exception:
            logger.exception("Failed to update the OCS access notice after OAuth")
    elif provider.startswith("commcare"):
        try:
            # allauth signal receivers are sync.
            async_to_sync(resolve_commcare_domains)(
                sociallogin.user, token.token, social_account=sociallogin.account
            )
        except Exception:
            logger.exception("Failed to resolve CommCare domains after OAuth")

    # Runs last, after tenant resolution and any B-merge (pre_social_login), so it
    # sees the user's fresh tenant access and all verified emails. Must not break
    # login on failure, same as the resolvers above.
    try:
        resolve_pending_invites_on_login(sociallogin.user)
    except Exception:
        logger.exception("Failed to resolve pending workspace invites after login")

    # /me caches a negative onboarding flag; without this, a user who just connected
    # their first data source is sent back to onboarding until it expires.
    try:
        cache.delete(me_onboarding_cache_key(sociallogin.user))
    except Exception:
        logger.warning("Failed to clear the onboarding cache after login", exc_info=True)


def _expire_invite(invite):
    # Conditional, like the awaiting-access move: keep a revoke's audit trail.
    WorkspaceInvite.objects.filter(pk=invite.pk, status=invite.status).update(
        status=WorkspaceInviteStatus.EXPIRED, updated_at=timezone.now()
    )


def _refreshed_expiry(invite):
    invite.refresh_from_db(fields=["expires_at"])
    return invite


def resolve_pending_invites_on_login(user):
    """Materialize any live WorkspaceInvite addressed to *user* into a membership.

    An invite is pure pre-authorization — it carries no data access (Root Cause
    A's access.py is the sole gate). This flips it into a real WorkspaceMembership
    only once the user can use EVERY one of the workspace's tenants (#381), and
    matches strictly on PROVEN emails so an unverified address can't claim one.
    """
    emails = proven_emails(user)
    if not emails:
        return

    invites = WorkspaceInvite.objects.filter(
        email__in=emails, status__in=LIVE_INVITE_STATUSES
    ).select_related("workspace")
    for invite in invites:
        if invite.is_expired:
            _expire_invite(invite)
            continue

        if accept_invite_if_covered(invite, user) is not None:
            notify_invite_accepted(invite, user)
        elif _refreshed_expiry(invite).is_expired:
            # Lapsed (or was shortened) before the workspace lock; dead, not awaiting access.
            _expire_invite(invite)
        elif invite.status != WorkspaceInviteStatus.AWAITING_ACCESS:
            # Conditional: a revoke since the read above must not be overwritten (#561 G4).
            moved = WorkspaceInvite.objects.filter(pk=invite.pk, status=invite.status).update(
                status=WorkspaceInviteStatus.AWAITING_ACCESS, updated_at=timezone.now()
            )
            if moved:
                invite.status = WorkspaceInviteStatus.AWAITING_ACCESS
                notify_awaiting_access(invite, user)


def reconcile_existing_user_on_login(sender, request, sociallogin, **kwargs):
    """Bridge the gap where allauth's _lookup_by_socialaccount short-circuits.

    When an existing OAuth user logs in and the provider now returns an email
    that the User row doesn't yet have, either backfill it or merge into the
    user that already owns that email — but only an email the provider vouched
    for (``verified_social_email``).
    """
    user = sociallogin.user
    if user.pk is None:
        return  # brand-new user; allauth's _lookup_by_email handles it
    if user.email:
        return  # already has an email — nothing to reconcile
    extra_data = sociallogin.account.extra_data
    if not isinstance(extra_data, dict) or not extra_data.get("email"):
        return
    new_email = verified_social_email(sociallogin.account.provider, extra_data)
    if new_email is None:
        # An email the provider did not vouch for must neither become the
        # account's email nor pull it into another user's account.
        logger.info(
            "Not reconciling user=%s: %s did not verify the email it asserted",
            user.pk,
            sociallogin.account.provider,
        )
        return

    UserModel = get_user_model()
    canonical = UserModel.objects.filter(email__iexact=new_email).exclude(pk=user.pk).first()
    if canonical is None:
        user.email = new_email
        user.save(update_fields=["email"])
        return

    if not _canonical_provably_owns_email(canonical, new_email):
        logger.warning(
            "Refusing auto-merge: canonical user=%s has not proven ownership of %s "
            "(no verified EmailAddress and no SocialAccount vouching for it)",
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
