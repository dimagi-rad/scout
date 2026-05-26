"""Merge two duplicate User rows into one canonical row.

Used by:
- ``apps.users.signals.reconcile_existing_user_on_login`` to absorb a freshly
  email-bearing OAuth user into an existing email-owning user during login.
- ``manage.py merge_duplicate_users`` for operator-driven cleanup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.db import transaction

from apps.users.models import TenantMembership
from apps.workspaces.models import WorkspaceMembership, WorkspaceRole

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)


_ROLE_RANK = {
    WorkspaceRole.READ: 0,
    WorkspaceRole.READ_WRITE: 1,
    WorkspaceRole.MANAGE: 2,
}


_SPECIAL_CASE_MODELS = frozenset({
    "socialaccount.SocialAccount",
    "account.EmailAddress",
    "users.TenantMembership",
    "workspaces.WorkspaceMembership",
})


def _repoint_long_tail_fks(canonical: User, duplicate: User) -> dict[str, int]:
    """Bulk-update every User FK except those handled by special-case logic.

    Picks up new User FKs added by future apps without code changes. Uses
    ``get_fields(include_hidden=True)`` so relations declared with
    ``related_name="+"`` (e.g. ``Workspace.created_by``) are also repointed.
    If a new FK has a unique constraint involving the user field, this raises
    IntegrityError on the bulk update — surface that and add explicit
    handling to merge_users.
    """
    counts: dict[str, int] = {}
    for rel in canonical._meta.get_fields(include_hidden=True):
        # Only one-to-many reverse relations (FKs pointing at User).
        if not rel.is_relation or not rel.one_to_many:
            continue
        label = rel.related_model._meta.label
        if label in _SPECIAL_CASE_MODELS:
            continue
        field_name = rel.field.name
        n = rel.related_model._default_manager.filter(
            **{field_name: duplicate},
        ).update(**{field_name: canonical})
        if n:
            counts[f"{label}.{field_name}"] = n
    return counts


@dataclass
class MergeReport:
    """Per-merge summary used for logging and command output."""

    canonical_id: int
    duplicate_id: int
    dry_run: bool = False
    field_changes: dict[str, str] = field(default_factory=dict)
    socialaccount_repointed: int = 0
    emailaddress_repointed: int = 0
    emailaddress_deleted: int = 0
    tenant_membership_repointed: int = 0
    tenant_membership_conflict_deleted: int = 0
    workspace_membership_repointed: int = 0
    workspace_membership_conflict_merged: int = 0
    long_tail_fk_counts: dict[str, int] = field(default_factory=dict)
    duplicate_user_deleted: bool = False


def select_canonical(users: list[User]) -> User:
    """Return the canonical user from a list of duplicates.

    Priority (highest wins): has a usable password, oldest ``created_at``,
    lowest ``pk``.
    """
    return min(
        users,
        key=lambda u: (
            not u.has_usable_password(),
            u.created_at,
            u.pk,
        ),
    )


def _repoint_social_accounts(canonical: User, duplicate: User) -> int:
    return SocialAccount.objects.filter(user=duplicate).update(user=canonical)


def _dedupe_email_addresses(canonical: User, duplicate: User) -> tuple[int, int]:
    """Returns (repointed_count, deleted_count).

    For each EmailAddress on duplicate: if canonical already has a row with the
    same email, delete duplicate's; otherwise repoint to canonical. Then ensure
    canonical has exactly one primary+verified row matching User.email.
    """
    canonical_emails = set(
        EmailAddress.objects.filter(user=canonical).values_list("email", flat=True)
    )
    dup_qs = EmailAddress.objects.filter(user=duplicate)
    deleted, _ = dup_qs.filter(email__in=canonical_emails).delete()
    repointed = dup_qs.exclude(email__in=canonical_emails).update(user=canonical)
    if canonical.email:
        # Demote any existing primaries on canonical before creating/promoting
        # the canonical-email row, to avoid violating unique(user_id, primary=True).
        EmailAddress.objects.filter(user=canonical, primary=True).exclude(
            email=canonical.email,
        ).update(primary=False)
        primary, _created = EmailAddress.objects.get_or_create(
            user=canonical, email=canonical.email,
            defaults={"primary": True, "verified": True},
        )
        if not primary.primary or not primary.verified:
            primary.primary = True
            primary.verified = True
            primary.save(update_fields=["primary", "verified"])
    return repointed, deleted


def _merge_tenant_memberships(canonical: User, duplicate: User) -> tuple[int, int]:
    """Returns (repointed_count, conflict_deleted_count).

    If canonical already has a membership for a given tenant, delete duplicate's
    row (its OneToOne TenantCredential cascades). Otherwise repoint to canonical.
    """
    canonical_tenant_ids = set(
        TenantMembership.objects.filter(user=canonical).values_list("tenant_id", flat=True)
    )
    dup_qs = TenantMembership.objects.filter(user=duplicate)
    conflict_deleted, _ = dup_qs.filter(tenant_id__in=canonical_tenant_ids).delete()
    repointed = dup_qs.exclude(tenant_id__in=canonical_tenant_ids).update(user=canonical)
    return repointed, conflict_deleted


def _merge_workspace_memberships(canonical: User, duplicate: User) -> tuple[int, int]:
    """Returns (repointed_count, conflict_merged_count).

    If canonical already has a membership for a workspace, upgrade its role to
    the higher of the two (per _ROLE_RANK) and delete the duplicate's row.
    Otherwise repoint duplicate's row to canonical.
    """
    canonical_by_ws = {
        m.workspace_id: m for m in WorkspaceMembership.objects.filter(user=canonical)
    }
    dup_memberships = list(WorkspaceMembership.objects.filter(user=duplicate))
    conflict_merged = 0
    repointed = 0
    for dup_m in dup_memberships:
        canon_m = canonical_by_ws.get(dup_m.workspace_id)
        if canon_m is None:
            dup_m.user = canonical
            dup_m.save(update_fields=["user"])
            repointed += 1
            continue
        if _ROLE_RANK[WorkspaceRole(dup_m.role)] > _ROLE_RANK[WorkspaceRole(canon_m.role)]:
            canon_m.role = dup_m.role
            canon_m.save(update_fields=["role"])
        dup_m.delete()
        conflict_merged += 1
    return repointed, conflict_merged


def _merge_user_fields(canonical: User, duplicate: User) -> dict[str, str]:
    """Apply field-level merge rules. Mutates canonical in place; returns changes."""
    changes: dict[str, str] = {}
    if not canonical.has_usable_password() and duplicate.has_usable_password():
        canonical.password = duplicate.password
        changes["password"] = "copied from duplicate"  # noqa: S105
    if duplicate.is_staff and not canonical.is_staff:
        canonical.is_staff = True
        changes["is_staff"] = "True (OR with duplicate)"
    if duplicate.is_superuser and not canonical.is_superuser:
        canonical.is_superuser = True
        changes["is_superuser"] = "True (OR with duplicate)"
    if duplicate.last_login and (
        canonical.last_login is None or duplicate.last_login > canonical.last_login
    ):
        canonical.last_login = duplicate.last_login
        changes["last_login"] = duplicate.last_login.isoformat()
    for field_name in ("first_name", "last_name", "avatar_url"):
        if not getattr(canonical, field_name) and getattr(duplicate, field_name):
            setattr(canonical, field_name, getattr(duplicate, field_name))
            changes[field_name] = f"copied: {getattr(duplicate, field_name)!r}"
    if canonical.timezone == "UTC" and duplicate.timezone and duplicate.timezone != "UTC":
        canonical.timezone = duplicate.timezone
        changes["timezone"] = f"copied: {duplicate.timezone!r}"
    if changes:
        canonical.save(update_fields=list(changes.keys()))
    return changes


def merge_users(
    *,
    canonical: User,
    duplicate: User,
    dry_run: bool = False,
) -> MergeReport:
    """Merge ``duplicate`` into ``canonical`` and return a MergeReport.

    Implementation arrives across the remaining tasks in this phase.
    """
    if canonical.pk == duplicate.pk:
        raise ValueError("canonical and duplicate must be different users")
    report = MergeReport(
        canonical_id=canonical.pk,
        duplicate_id=duplicate.pk,
        dry_run=dry_run,
    )
    if dry_run:
        report.socialaccount_repointed = SocialAccount.objects.filter(user=duplicate).count()
        canonical_emails = set(
            EmailAddress.objects.filter(user=canonical).values_list("email", flat=True)
        )
        dup_qs = EmailAddress.objects.filter(user=duplicate)
        report.emailaddress_deleted = dup_qs.filter(email__in=canonical_emails).count()
        report.emailaddress_repointed = dup_qs.exclude(email__in=canonical_emails).count()
        canonical_tenant_ids = set(
            TenantMembership.objects.filter(user=canonical).values_list("tenant_id", flat=True)
        )
        dup_tms = TenantMembership.objects.filter(user=duplicate)
        report.tenant_membership_conflict_deleted = dup_tms.filter(
            tenant_id__in=canonical_tenant_ids,
        ).count()
        report.tenant_membership_repointed = dup_tms.exclude(
            tenant_id__in=canonical_tenant_ids,
        ).count()
        canonical_ws_ids = set(
            WorkspaceMembership.objects.filter(user=canonical).values_list(
                "workspace_id", flat=True,
            )
        )
        dup_wms = WorkspaceMembership.objects.filter(user=duplicate)
        report.workspace_membership_conflict_merged = dup_wms.filter(
            workspace_id__in=canonical_ws_ids,
        ).count()
        report.workspace_membership_repointed = dup_wms.exclude(
            workspace_id__in=canonical_ws_ids,
        ).count()
        return report
    with transaction.atomic():
        report.field_changes = _merge_user_fields(canonical, duplicate)
        report.socialaccount_repointed = _repoint_social_accounts(canonical, duplicate)
        report.emailaddress_repointed, report.emailaddress_deleted = (
            _dedupe_email_addresses(canonical, duplicate)
        )
        report.tenant_membership_repointed, report.tenant_membership_conflict_deleted = (
            _merge_tenant_memberships(canonical, duplicate)
        )
        report.workspace_membership_repointed, report.workspace_membership_conflict_merged = (
            _merge_workspace_memberships(canonical, duplicate)
        )
        report.long_tail_fk_counts = _repoint_long_tail_fks(canonical, duplicate)
    return report
