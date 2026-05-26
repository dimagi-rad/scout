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
    return MergeReport(
        canonical_id=canonical.pk,
        duplicate_id=duplicate.pk,
        dry_run=dry_run,
    )
