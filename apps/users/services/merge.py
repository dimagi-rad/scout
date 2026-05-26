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

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)


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


def _merge_user_fields(canonical, duplicate) -> dict[str, str]:
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
    canonical.save()
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
        return report
    report.field_changes = _merge_user_fields(canonical, duplicate)
    return report
