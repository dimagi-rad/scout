"""Compose and dispatch WorkspaceInvite notifications.

Data-source naming is generic (never Connect-specific): the phrasing is derived
from each tenant's ``provider`` + ``canonical_name`` so the same code serves
CommCare Connect opportunities, Open Chat Studio bots, and CommCare HQ projects.
Delivery goes through the async ``send_email`` task off the request path.
"""

import logging

from django.conf import settings

from apps.users.tasks import send_email

logger = logging.getLogger(__name__)

# (product name, noun) per provider — the generic label building blocks.
_PROVIDER_SOURCE_NOUNS = {
    "commcare_connect": ("CommCare Connect", "opportunity"),
    "ocs": ("Open Chat Studio", "bot"),
    "commcare": ("CommCare HQ", "project"),
}


def describe_workspace_sources(workspace) -> str:
    """A human phrase for the upstream data source(s) a workspace draws from,
    e.g. "the CommCare Connect opportunity 'Malaria Study'". Joined with "and":
    a member needs every one of them (#380)."""
    labels = []
    for wt in workspace.workspace_tenants.select_related("tenant"):
        tenant = wt.tenant
        product, noun = _PROVIDER_SOURCE_NOUNS.get(
            tenant.provider, (tenant.provider, "data source")
        )
        labels.append(f"the {product} {noun} '{tenant.canonical_name}'")
    if not labels:
        return "this workspace's data source"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _invite_link(invite) -> str:
    return f"{settings.SCOUT_BASE_URL.rstrip('/')}/?invite={invite.token}"


def _user_label(user) -> str:
    # Email is nullable, so a nameless, email-less user must still get the fallback.
    return (user and (user.get_full_name() or user.email)) or "A Scout workspace manager"


def _workspace_link(workspace) -> str:
    return f"{settings.SCOUT_BASE_URL.rstrip('/')}/workspaces/{workspace.id}/chat"


def _inviter_label(invite) -> str:
    return _user_label(invite.invited_by)


def _dispatch(subject, message, recipient_list):
    """Enqueue an email; a delivery/enqueue failure must never break the caller
    (an invite flow or the login signal)."""
    try:
        send_email.defer(subject=subject, message=message, recipient_list=recipient_list)
    except Exception:
        logger.exception("Failed to enqueue invite email to %s", recipient_list)


def send_pending_invite_email(invite):
    """Phase 2: tell someone with no Scout account they've been invited."""
    workspace_name = invite.workspace.name
    subject = f"You've been invited to '{workspace_name}' on Scout"
    message = (
        f"{_inviter_label(invite)} invited you to the '{workspace_name}' workspace on Scout.\n\n"
        f"Sign in to accept: {_invite_link(invite)}\n"
    )
    _dispatch(subject, message, [invite.email])


def notify_awaiting_access(invite, invitee, *, notify_manager=True):
    """Bidirectional 'logged in but no upstream access' notice.

    The in-app surface for the invitee is served separately (the awaiting_access
    invite rows are queryable at render time); this is the email half.
    """
    workspace_name = invite.workspace.name
    source = describe_workspace_sources(invite.workspace)
    _dispatch(
        f"Action needed to access '{workspace_name}' on Scout",
        (
            f"You were invited to '{workspace_name}' on Scout, which needs access to "
            f"{source}. You can't use all of that yet. Ask to be added where you're missing, "
            f"then connect it in Connected Accounts — Scout unlocks the workspace "
            f"automatically once you can use every source.\n"
        ),
        [invitee.email],
    )
    inviter = invite.invited_by
    if notify_manager and inviter and inviter.email:
        _dispatch(
            f"{invitee.email} can't yet access '{workspace_name}'",
            (
                f"{invitee.email} signed into Scout but can't use all of {source}, so they "
                f"still can't see the data. Grant them access in the source systems they're "
                f"missing and it resolves automatically.\n"
            ),
            [inviter.email],
        )


def notify_invite_accepted(invite, invitee):
    """Happy path: access materialized, invite became a membership."""
    workspace_name = invite.workspace.name
    _dispatch(
        f"You now have access to '{workspace_name}' on Scout",
        f"You're in — you now have access to the '{workspace_name}' workspace on Scout.\n",
        [invitee.email],
    )
    inviter = invite.invited_by
    if inviter and inviter.email:
        _dispatch(
            f"{invitee.email} now has access to '{workspace_name}'",
            f"{invitee.email} now has access to '{workspace_name}' on Scout.\n",
            [inviter.email],
        )


def notify_member_added(membership, added_by):
    """Tell a user a manager added them straight to a workspace (#382): the direct
    path creates a membership with no invite, so no other notice ever reaches them."""
    workspace = membership.workspace
    link = _workspace_link(workspace)
    _dispatch(
        f"You've been added to '{workspace.name}' on Scout",
        (
            f"{_user_label(added_by)} added you to the '{workspace.name}' "
            f"workspace on Scout.\n\nOpen it: {link}\n"
        ),
        [membership.user.email],
    )


# Matches the role names the members UI shows, not the model's choice labels.
_ROLE_LABELS = {"read": "Read", "read_write": "Read-Write", "manage": "Manager"}


def _role_label(role) -> str:
    return _ROLE_LABELS.get(role, role)


def notify_role_changed(membership, changed_by):
    """Tell a member a manager changed their role (#382)."""
    workspace = membership.workspace
    user = membership.user
    if not user.email:
        return
    _dispatch(
        f"Your role in '{workspace.name}' on Scout changed",
        (
            f"{_user_label(changed_by)} changed your role in the '{workspace.name}' "
            f"workspace on Scout to {_role_label(membership.role)}.\n\n"
            f"Open it: {_workspace_link(workspace)}\n"
        ),
        [user.email],
    )


def notify_member_removed(workspace, user, removed_by):
    """Tell a user a manager removed them from a workspace (#382). Removal also
    deletes their conversations there, so the notice says so."""
    if not user.email:
        return
    _dispatch(
        f"You've been removed from '{workspace.name}' on Scout",
        (
            f"{_user_label(removed_by)} removed you from the '{workspace.name}' workspace "
            f"on Scout. Your conversations in it have been deleted.\n"
        ),
        [user.email],
    )


def notify_invite_revoked(invite, revoked_by):
    """Tell an invitee their invite was withdrawn (#382): the invite and awaiting-access
    emails may have sent them off to sign in or get upstream access."""
    workspace_name = invite.workspace.name
    _dispatch(
        f"Your invite to '{workspace_name}' on Scout was withdrawn",
        (
            f"{_user_label(revoked_by)} withdrew your invite to the '{workspace_name}' "
            f"workspace on Scout. You don't need to do anything.\n"
        ),
        [invite.email],
    )
