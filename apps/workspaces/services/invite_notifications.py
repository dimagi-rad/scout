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
    e.g. "the CommCare Connect opportunity 'Malaria Study'"."""
    labels = []
    for wt in workspace.workspace_tenants.select_related("tenant"):
        tenant = wt.tenant
        product, noun = _PROVIDER_SOURCE_NOUNS.get(tenant.provider, (tenant.provider, "data source"))
        labels.append(f"the {product} {noun} '{tenant.canonical_name}'")
    if not labels:
        return "this workspace's data source"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " or " + labels[-1]


def _invite_link(invite) -> str:
    return f"{settings.SCOUT_BASE_URL.rstrip('/')}/?invite={invite.token}"


def _inviter_label(invite) -> str:
    inviter = invite.invited_by
    if not inviter:
        return "A Scout workspace manager"
    return inviter.get_full_name() or inviter.email


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
            f"You were invited to '{workspace_name}' on Scout, but you don't yet have access "
            f"to {source}. Ask to be added there — Scout unlocks it automatically once you do.\n"
        ),
        [invitee.email],
    )
    inviter = invite.invited_by
    if notify_manager and inviter and inviter.email:
        _dispatch(
            f"{invitee.email} can't yet access '{workspace_name}'",
            (
                f"{invitee.email} signed into Scout but doesn't have access to {source}, so they "
                f"still can't see the data. Grant them access in the source system and it "
                f"resolves automatically.\n"
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
