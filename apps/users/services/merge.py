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

from apps.users.models import TenantConnection, TenantMembership
from apps.workspaces.models import TenantMetadata, WorkspaceMembership, WorkspaceRole

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)


_ROLE_RANK = {
    WorkspaceRole.READ: 0,
    WorkspaceRole.READ_WRITE: 1,
    WorkspaceRole.MANAGE: 2,
}


# Reverse FK relations handled by explicit helpers above. Anything else with a
# User FK gets the generic bulk-repoint in _repoint_long_tail_fks.
# Identified by (model label, field name) so we can skip the canonical
# `user` field on a model while still picking up other User-pointing fields
# on the same model (e.g. WorkspaceMembership.invited_by).
_SPECIAL_CASE_RELATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("socialaccount.SocialAccount", "user"),
        ("account.EmailAddress", "user"),
        ("users.TenantMembership", "user"),
        ("users.TenantConnection", "user"),
        ("workspaces.WorkspaceMembership", "user"),
        # Django auth join tables — auto-generated through-tables for the custom
        # User model. Unique on (user, group) / (user, permission); a bulk repoint
        # would IntegrityError if both users share a group/permission. Scout
        # doesn't actively use django.contrib.auth.Group, so the simplest safe
        # handling is to skip; if Group ever becomes load-bearing we revisit
        # with explicit conflict resolution.
        ("users.User_groups", "user"),
        ("users.User_user_permissions", "user"),
    }
)


def _repoint_long_tail_fks(canonical: User, duplicate: User) -> dict[str, int]:
    """Bulk-update every User reverse FK except those handled by explicit helpers.

    Skips the (model, field) pairs in _SPECIAL_CASE_RELATIONS. Picks up new
    User FKs added by future apps without code changes. If a new FK has a
    unique constraint involving the user field, the bulk update will raise
    IntegrityError — surface that and add explicit handling.
    """
    counts: dict[str, int] = {}
    for rel in canonical._meta.get_fields(include_hidden=True):
        if not (rel.is_relation and rel.one_to_many):
            continue
        label = rel.related_model._meta.label
        field_name = rel.field.name
        if (label, field_name) in _SPECIAL_CASE_RELATIONS:
            continue
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
    tenant_membership_metadata_migrated: int = 0
    tenant_connection_repointed: int = 0
    tenant_connection_conflict_merged: int = 0
    workspace_membership_repointed: int = 0
    workspace_membership_conflict_merged: int = 0
    long_tail_fk_counts: dict[str, int] = field(default_factory=dict)
    duplicate_user_deleted: bool = False
    # 11#4: privilege flags the discarded duplicate held that the canonical did
    # NOT — surfaced for the operator/log but never silently applied (no
    # escalation from the discarded account).
    discarded_privileges: set[str] = field(default_factory=set)


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
            user=canonical,
            email=canonical.email,
            defaults={"primary": True, "verified": True},
        )
        if not primary.primary or not primary.verified:
            primary.primary = True
            primary.verified = True
            primary.save(update_fields=["primary", "verified"])
    return repointed, deleted


def _migrate_conflicting_membership(canon_m: TenantMembership, dup_m: TenantMembership) -> int:
    """Carry discovered data from a conflicting duplicate membership onto the
    canonical's surviving membership, when the canonical lacks it.

    04#1: ``TenantMetadata`` is a OneToOne(CASCADE) on ``TenantMembership`` and
    ``provider_metadata``/``connection`` ride on the membership row. Deleting the
    duplicate's conflicting membership cascade-deletes its ``TenantMetadata`` and
    drops its connection wiring. Before that delete, migrate each of those onto
    the canonical membership *only when the canonical doesn't already have it* —
    never clobbering the canonical's own discovered data.

    Returns 1 if any ``TenantMetadata`` row was migrated, else 0.
    """
    canon_changed_fields: list[str] = []

    # provider_metadata (JSON on the membership): backfill if canonical's is empty.
    if not canon_m.provider_metadata and dup_m.provider_metadata:
        canon_m.provider_metadata = dup_m.provider_metadata
        canon_changed_fields.append("provider_metadata")

    # connection wiring: adopt the duplicate's connection if the canonical's
    # membership is unwired. (_merge_tenant_connections repoints the connection
    # row itself to the canonical user; here we wire the surviving membership to
    # it so the canonical isn't left connection=None.)
    if canon_m.connection_id is None and dup_m.connection_id is not None:
        canon_m.connection_id = dup_m.connection_id
        canon_changed_fields.append("connection")

    if canon_changed_fields:
        canon_m.save(update_fields=canon_changed_fields)

    # TenantMetadata (OneToOne, CASCADE): migrate the duplicate's onto the
    # canonical membership when the canonical has none. Otherwise leave the
    # duplicate's to cascade away (the canonical's is authoritative).
    metadata_migrated = 0
    dup_metadata = TenantMetadata.objects.filter(tenant_membership=dup_m).first()
    if (
        dup_metadata is not None
        and not TenantMetadata.objects.filter(tenant_membership=canon_m).exists()
    ):
        dup_metadata.tenant_membership = canon_m
        dup_metadata.save(update_fields=["tenant_membership"])
        metadata_migrated = 1

    return metadata_migrated


def _merge_tenant_memberships(canonical: User, duplicate: User) -> tuple[int, int, int]:
    """Returns (repointed_count, conflict_deleted_count, metadata_migrated_count).

    If canonical already has a membership for a given tenant, the duplicate's
    conflicting row is deleted — but first any discovered data riding on it
    (TenantMetadata, provider_metadata, connection wiring) is migrated onto the
    canonical's membership when the canonical lacks it (04#1), so cascade-delete
    never destroys the only copy. Otherwise the duplicate's membership is
    repointed to canonical. (Connection *rows* are merged separately by
    _merge_tenant_connections, which the surviving memberships still reference.)
    """
    canon_by_tenant = {m.tenant_id: m for m in TenantMembership.objects.filter(user=canonical)}
    repointed = 0
    conflict_deleted = 0
    metadata_migrated = 0
    for dup_m in TenantMembership.objects.filter(user=duplicate):
        canon_m = canon_by_tenant.get(dup_m.tenant_id)
        if canon_m is None:
            dup_m.user = canonical
            dup_m.save(update_fields=["user"])
            repointed += 1
            continue
        metadata_migrated += _migrate_conflicting_membership(canon_m, dup_m)
        dup_m.delete()
        conflict_deleted += 1
    return repointed, conflict_deleted, metadata_migrated


def _merge_tenant_connections(canonical: User, duplicate: User) -> tuple[int, int]:
    """Returns (repointed_count, conflict_merged_count).

    Repoint the duplicate's connections to canonical. The model allows only one
    OAuth connection per (user, provider), so when canonical already owns the
    OAuth connection for a provider, the duplicate's OAuth connection for that
    provider is merged: its memberships are repointed to canonical's connection
    and the duplicate row is deleted. API-key connections are always repointed.
    """
    canonical_oauth = {
        c.provider: c
        for c in TenantConnection.objects.filter(
            user=canonical, credential_type=TenantConnection.OAUTH
        )
    }
    repointed = 0
    conflict_merged = 0
    for conn in TenantConnection.objects.filter(user=duplicate):
        existing = canonical_oauth.get(conn.provider)
        if conn.credential_type == TenantConnection.OAUTH and existing is not None:
            conn.memberships.update(connection=existing)
            conn.delete()
            conflict_merged += 1
        else:
            conn.user = canonical
            conn.save(update_fields=["user"])
            repointed += 1
    return repointed, conflict_merged


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


def _discarded_privileges(canonical: User, duplicate: User) -> set[str]:
    """Privilege flags the duplicate holds that the canonical does not.

    11#4: these are NEVER propagated onto the canonical (no silent escalation
    from a discarded account). They are returned only so the operator command
    and the merge log can surface that the deleted duplicate was privileged.
    """
    discarded: set[str] = set()
    for flag in ("is_staff", "is_superuser"):
        if getattr(duplicate, flag) and not getattr(canonical, flag):
            discarded.add(flag)
    return discarded


def _merge_user_fields(canonical: User, duplicate: User) -> dict[str, str]:
    """Apply field-level merge rules. Mutates canonical in place; returns changes.

    NOTE (11#4): ``is_staff``/``is_superuser`` are deliberately NOT merged. The
    canonical keeps its own privilege flags unchanged so that absorbing a stale
    ``createsuperuser`` dev artifact (or any privileged duplicate) can never
    silently promote a normal account to production admin. Use
    ``_discarded_privileges`` to report what the discarded duplicate held.
    """
    changes: dict[str, str] = {}
    if not canonical.has_usable_password() and duplicate.has_usable_password():
        canonical.password = duplicate.password
        changes["password"] = "copied from duplicate"  # noqa: S105
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

    Field-level rules are applied to ``canonical`` (password copy,
    oldest-last-login, empty-name backfill, timezone backfill).
    ``is_staff``/``is_superuser`` are intentionally NOT merged (11#4: never
    escalate the canonical from the discarded duplicate); any privilege the
    duplicate held that the canonical lacks is reported in
    ``MergeReport.discarded_privileges``. All User-pointing rows are then
    repointed or merged: SocialAccount,
    EmailAddress (with dedupe + primary normalization), TenantMembership and
    WorkspaceMembership (with conflict resolution), and every other
    reverse-FK relation discovered via Django's `_meta` (the "long tail"). The
    duplicate row is deleted last. All write steps run inside a single
    ``transaction.atomic()`` block so any failure rolls the whole merge back.

    When ``dry_run=True`` no writes occur and the returned MergeReport carries
    counts only — useful for previewing the impact from the operator command.
    """
    if canonical.pk == duplicate.pk:
        raise ValueError("canonical and duplicate must be different users")
    report = MergeReport(
        canonical_id=canonical.pk,
        duplicate_id=duplicate.pk,
        dry_run=dry_run,
    )
    if dry_run:
        report.discarded_privileges = _discarded_privileges(canonical, duplicate)
        report.socialaccount_repointed = SocialAccount.objects.filter(user=duplicate).count()
        canonical_emails = set(
            EmailAddress.objects.filter(user=canonical).values_list("email", flat=True)
        )
        dup_emails = EmailAddress.objects.filter(user=duplicate)
        report.emailaddress_deleted = dup_emails.filter(email__in=canonical_emails).count()
        report.emailaddress_repointed = dup_emails.exclude(email__in=canonical_emails).count()
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
        canonical_oauth_providers = set(
            TenantConnection.objects.filter(
                user=canonical, credential_type=TenantConnection.OAUTH
            ).values_list("provider", flat=True)
        )
        dup_conns = TenantConnection.objects.filter(user=duplicate)
        report.tenant_connection_conflict_merged = dup_conns.filter(
            credential_type=TenantConnection.OAUTH, provider__in=canonical_oauth_providers
        ).count()
        report.tenant_connection_repointed = (
            dup_conns.count() - report.tenant_connection_conflict_merged
        )
        canonical_ws_ids = set(
            WorkspaceMembership.objects.filter(user=canonical).values_list(
                "workspace_id",
                flat=True,
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
        report.discarded_privileges = _discarded_privileges(canonical, duplicate)
        report.field_changes = _merge_user_fields(canonical, duplicate)
        report.socialaccount_repointed = _repoint_social_accounts(canonical, duplicate)
        report.emailaddress_repointed, report.emailaddress_deleted = _dedupe_email_addresses(
            canonical, duplicate
        )
        (
            report.tenant_membership_repointed,
            report.tenant_membership_conflict_deleted,
            report.tenant_membership_metadata_migrated,
        ) = _merge_tenant_memberships(canonical, duplicate)
        report.tenant_connection_repointed, report.tenant_connection_conflict_merged = (
            _merge_tenant_connections(canonical, duplicate)
        )
        report.workspace_membership_repointed, report.workspace_membership_conflict_merged = (
            _merge_workspace_memberships(canonical, duplicate)
        )
        report.long_tail_fk_counts = _repoint_long_tail_fks(canonical, duplicate)
        duplicate.delete()
        report.duplicate_user_deleted = True
    logger.info(
        "Merged user=%s into canonical=%s: %s",
        report.duplicate_id,
        report.canonical_id,
        report,
    )
    return report
