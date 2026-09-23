"""Admission checks that keep every workspace member covering every tenant (#381).

The read gate (``apps.workspaces.access``) denies a member who cannot use one of
the workspace's tenants; these checks refuse the membership or source change that
would create that state in the first place. They are strictly all-of regardless
of the read gate's rollout switch, so while the switch is off a partially covering
user is no longer admitted (they get an awaiting-access invite instead of any-of
access). That is deliberate: every gap admitted now is a member the flip takes
dark later (#381).

Final checks and mutations run under a lock on the workspace row, shared by every
admission mutation here, so a concurrent member add and source add cannot each pass
against the state the other is about to change. Source removal does not take it;
that race can only refuse an admission, never admit a gap. Upstream refreshes
happen before the lock, never inside it.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.workspaces.access import _workspace_tenants
from apps.workspaces.models import (
    Workspace,
    WorkspaceInviteStatus,
    WorkspaceMembership,
    WorkspaceTenant,
)
from apps.workspaces.services.credential_coverage import (
    MissingTenant,
    coverage_gaps,
    member_coverage_gaps,
)
from apps.workspaces.services.workspace_service import add_workspace_tenant


class MembersLackTenant(Exception):
    """Adding the tenant would leave these ``(user, MissingTenant)`` members uncovered."""

    def __init__(self, gaps):
        super().__init__("Workspace members lack the tenant being added.")
        self.gaps = gaps


def _lock(workspace) -> None:
    Workspace.objects.select_for_update().only("pk").get(pk=workspace.pk)


def missing_for_user(user, workspace) -> tuple[MissingTenant, ...]:
    """Workspace tenants ``user`` cannot use with their own credential."""
    return tuple(member_coverage_gaps(user.pk, _workspace_tenants(workspace)).values())


def members_lacking_tenant(workspace, tenant) -> list[tuple]:
    """Current members who cannot use ``tenant``, as ``(user, MissingTenant)``."""
    members = [
        m.user
        for m in WorkspaceMembership.objects.filter(workspace=workspace)
        .select_related("user")
        .order_by("user__email")
    ]
    gaps = coverage_gaps((member.pk, tenant) for member in members)
    key = str(tenant.pk)
    return [(member, gaps[(member.pk, key)]) for member in members if (member.pk, key) in gaps]


def add_tenant_covered_by_members(workspace, tenant):
    """Add ``tenant`` only if every current member can use it.

    Returns ``(WorkspaceTenant, created)``; re-adding an existing tenant is a
    no-op that does not re-check members. Raises ``MembersLackTenant`` naming who
    is uncovered — never evicts anyone to make the addition succeed.
    """
    with transaction.atomic():
        _lock(workspace)
        existing = WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).first()
        if existing is not None:
            return existing, False
        lacking = members_lacking_tenant(workspace, tenant)
        if lacking:
            raise MembersLackTenant(lacking)
        return add_workspace_tenant(workspace, tenant)


def admit_covered_member(workspace, user, *, role, invited_by):
    """Create ``user``'s membership only if they cover every workspace tenant.

    Returns ``(membership, created, missing)``. With gaps, nothing is written and
    ``missing`` names them so the caller can leave an awaiting-access invite. An
    existing membership is returned untouched — never re-roled by an add.
    """
    with transaction.atomic():
        _lock(workspace)
        # Existing members first: one who has since lost a source already has the
        # denial and its remedy, and must not also be sent an invite to rejoin.
        # authz-exempt: admission of the TARGET, not an access decision for a requester.
        existing = WorkspaceMembership.objects.filter(workspace=workspace, user=user).first()
        if existing is not None:
            return existing, False, ()
        missing = missing_for_user(user, workspace)
        if missing:
            return None, False, missing
        membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=user, role=role, invited_by=invited_by
        )
        return membership, True, ()


def accept_invite_if_covered(invite, user):
    """Turn ``invite`` into a membership once ``user`` covers every tenant.

    Returns the membership, or ``None`` when coverage is still incomplete. An
    existing membership keeps its role: accepting a stale invite never promotes.
    """
    with transaction.atomic():
        _lock(invite.workspace)
        if missing_for_user(user, invite.workspace):
            return None
        membership, _ = WorkspaceMembership.objects.get_or_create(
            workspace=invite.workspace,
            user=user,
            defaults={"role": invite.role, "invited_by": invite.invited_by},
        )
        invite.status = WorkspaceInviteStatus.ACCEPTED
        invite.resolved_at = timezone.now()
        invite.resolved_membership = membership
        invite.save(update_fields=["status", "resolved_at", "resolved_membership", "updated_at"])
        return membership
