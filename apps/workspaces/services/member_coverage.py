"""Admission checks that keep every workspace member covering every tenant (#381).

The read gate (``apps.workspaces.access``) denies a member who cannot use one of
the workspace's tenants; these checks refuse the membership or source change that
would create that state in the first place. Admission is all-of even while the
read gate's rollout switch is off (``ADMISSION_ALWAYS_ALL_OF``), so no new gaps
accumulate before the flip: a partially covering user gets an awaiting-access
invite rather than any-of access.

Final checks and mutations run under a lock on the workspace row, shared by every
admission mutation here, so a concurrent member add and source add cannot each pass
against the state the other is about to change. Source removal does not take it;
that race can only refuse an admission, never admit a gap. Upstream refreshes
happen before the lock, never inside it.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.users.models import TenantMembership
from apps.workspaces.access import (
    _workspace_tenants,
    all_of_access_enforced,
    missing_workspace_tenants,
)
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


# Product decision (Brian, 2026-09-24): admission is strict ahead of the read flip.
# False would make admission follow the read gate's rollout switch instead.
ADMISSION_ALWAYS_ALL_OF = True


def admission_all_of() -> bool:
    return ADMISSION_ALWAYS_ALL_OF or all_of_access_enforced()


def requester_gaps(user, tenants) -> tuple[MissingTenant, ...]:
    """Tenants among ``tenants`` a requester may not attach, under the admission rule.

    Any-of admission only ever asked for a live membership on each tenant, so
    that is all it checks; all-of asks for a usable credential.
    """
    tenants = list(tenants)
    if not admission_all_of():
        live = set(
            TenantMembership.objects.filter(user=user, tenant__in=tenants).values_list(
                "tenant_id", flat=True
            )
        )
        tenants = [t for t in tenants if t.pk not in live]
    return tuple(member_coverage_gaps(user.pk, tenants).values()) if tenants else ()


def _lock(workspace) -> None:
    Workspace.objects.select_for_update().only("pk").get(pk=workspace.pk)


def missing_for_user(user, workspace) -> tuple[MissingTenant, ...]:
    """Workspace tenants keeping ``user`` out under the admission rule."""
    tenants = _workspace_tenants(workspace)
    if not admission_all_of():
        return missing_workspace_tenants(user, tenants)
    return tuple(member_coverage_gaps(user.pk, tenants).values())


def members_lacking_tenant(workspace, tenant) -> list[tuple]:
    """Current members who cannot use ``tenant``, as ``(user, MissingTenant)``.

    Any-of admission never consulted other members, so it finds none.
    """
    if not admission_all_of():
        return []
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
