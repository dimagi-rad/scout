"""Unit tests for apps.users.services.merge.merge_users and helpers."""

from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from apps.chat.models import Thread
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.users.services.merge import merge_users, select_canonical
from apps.workspaces.models import (
    TenantMetadata,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)

User = get_user_model()


@pytest.mark.django_db
def test_select_canonical_prefers_usable_password():
    no_pw = User.objects.create(email="a@y.com", username="a")
    no_pw.set_unusable_password()
    no_pw.save()
    with_pw = User.objects.create(email="b@y.com", username="b")
    with_pw.set_password("real-password")
    with_pw.save()

    assert select_canonical([no_pw, with_pw]) == with_pw


@pytest.mark.django_db
def test_select_canonical_prefers_oldest_when_passwords_equal():
    older = User.objects.create(email="older@y.com", username="older")
    older.set_password("pw")
    older.save()
    newer = User.objects.create(email="newer@y.com", username="newer")
    newer.set_password("pw")
    newer.save()

    assert select_canonical([newer, older]) == older


@pytest.mark.django_db
def test_field_level_merge_copies_password_from_duplicate():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    canonical.set_unusable_password()
    canonical.save()
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    duplicate.set_password("real-password")
    duplicate.save()
    dup_hash = duplicate.password

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.password == dup_hash
    assert canonical.has_usable_password()


@pytest.mark.django_db
def test_field_level_merge_never_escalates_staff_or_superuser_from_duplicate():
    """SECURITY (11#4): merging a privileged duplicate (e.g. a stale
    createsuperuser dev artifact) into a normal canonical must NOT silently
    promote the canonical. The canonical's own flags are kept; the discarded
    duplicate's flags are never OR-propagated."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    assert canonical.is_staff is False
    assert canonical.is_superuser is False
    duplicate = User.objects.create(
        email="dup@y.com",
        username="dup",
        is_staff=True,
        is_superuser=True,
    )

    report = merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    # No escalation: canonical stays exactly as privileged as it was.
    assert canonical.is_staff is False
    assert canonical.is_superuser is False
    assert "is_staff" not in report.field_changes
    assert "is_superuser" not in report.field_changes
    # The discarded privilege is surfaced (so the operator/log can see it) but
    # NOT applied.
    assert report.discarded_privileges == {"is_staff", "is_superuser"}


@pytest.mark.django_db
def test_field_level_merge_keeps_canonical_existing_privileges():
    """A canonical that is already staff/superuser stays so regardless of the
    duplicate; no privilege is dropped or surfaced as discarded."""
    canonical = User.objects.create(
        email="canon@y.com", username="canon", is_staff=True, is_superuser=True
    )
    duplicate = User.objects.create(email="dup@y.com", username="dup")

    report = merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.is_staff is True
    assert canonical.is_superuser is True
    assert report.discarded_privileges == set()


@pytest.mark.django_db
def test_field_level_merge_fills_empty_name_fields_from_duplicate():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(
        email="dup@y.com",
        username="dup",
        first_name="Brian",
        last_name="DeRenzi",
        avatar_url="https://x/y.png",
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.first_name == "Brian"
    assert canonical.last_name == "DeRenzi"
    assert canonical.avatar_url == "https://x/y.png"


@pytest.mark.django_db
def test_field_level_merge_keeps_canonical_name_when_already_set():
    canonical = User.objects.create(
        email="canon@y.com",
        username="canon",
        first_name="Already",
        last_name="Set",
    )
    duplicate = User.objects.create(
        email="dup@y.com",
        username="dup",
        first_name="Newer",
        last_name="Name",
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.first_name == "Already"
    assert canonical.last_name == "Set"


@pytest.mark.django_db
def test_socialaccounts_are_repointed_to_canonical():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    duplicate_pk = duplicate.pk
    SocialAccount.objects.create(user=duplicate, provider="commcare", uid="42")
    SocialAccount.objects.create(user=duplicate, provider="ocs", uid="ocs-7")
    SocialAccount.objects.create(user=canonical, provider="commcare_connect", uid="9")

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.socialaccount_repointed == 2
    assert SocialAccount.objects.filter(user=canonical).count() == 3
    assert SocialAccount.objects.filter(user_id=duplicate_pk).count() == 0


@pytest.mark.django_db
def test_emailaddress_dedupes_when_both_have_same_email():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email="brian-old@y.com", username="dup")
    EmailAddress.objects.create(user=canonical, email="brian@y.com", primary=True, verified=True)
    EmailAddress.objects.create(user=duplicate, email="brian@y.com", primary=True, verified=False)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.emailaddress_deleted == 1
    assert EmailAddress.objects.filter(email="brian@y.com").count() == 1
    survivor = EmailAddress.objects.get(email="brian@y.com")
    assert survivor.user == canonical
    assert survivor.primary is True
    assert survivor.verified is True


@pytest.mark.django_db
def test_emailaddress_repoints_distinct_addresses():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email="brian-old@y.com", username="dup")
    EmailAddress.objects.create(
        user=duplicate, email="brian-old@y.com", primary=True, verified=True
    )

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.emailaddress_repointed == 1
    assert EmailAddress.objects.filter(user=canonical, email="brian-old@y.com").exists()


@pytest.mark.django_db
def test_emailaddress_creates_primary_row_for_canonical_email():
    canonical = User.objects.create(email="brian@y.com", username="canon")
    duplicate = User.objects.create(email="brian-old@y.com", username="dup")
    # No EmailAddress rows for canonical; merge should create one.

    merge_users(canonical=canonical, duplicate=duplicate)

    primary = EmailAddress.objects.get(user=canonical, email="brian@y.com")
    assert primary.primary is True
    assert primary.verified is True


@pytest.mark.django_db
def test_tenantmembership_repoints_when_canonical_has_no_overlap():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    t1 = Tenant.objects.create(provider="commcare", external_id="d1", canonical_name="D1")
    t2 = Tenant.objects.create(provider="commcare", external_id="d2", canonical_name="D2")
    TenantMembership.objects.create(user=duplicate, tenant=t1)
    TenantMembership.objects.create(user=duplicate, tenant=t2)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.tenant_membership_repointed == 2
    assert report.tenant_membership_conflict_deleted == 0
    assert TenantMembership.objects.filter(user=canonical).count() == 2


@pytest.mark.django_db
def test_tenantmembership_conflict_keeps_canonical_and_deletes_duplicates():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    shared = Tenant.objects.create(provider="commcare", external_id="d1", canonical_name="D1")
    only_dup = Tenant.objects.create(provider="ocs", external_id="exp9", canonical_name="Exp9")
    canon_tm = TenantMembership.objects.create(user=canonical, tenant=shared)
    canon_conn = TenantConnection.objects.create(
        user=canonical,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
    )
    canon_tm.connection = canon_conn
    canon_tm.save(update_fields=["connection"])
    TenantMembership.objects.create(user=duplicate, tenant=shared)  # conflict
    TenantMembership.objects.create(user=duplicate, tenant=only_dup)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.tenant_membership_repointed == 1
    assert report.tenant_membership_conflict_deleted == 1
    # canonical still has its row for the shared tenant
    assert TenantMembership.objects.filter(user=canonical, tenant=shared).count() == 1
    # canonical now also has only_dup's tenant
    assert TenantMembership.objects.filter(user=canonical, tenant=only_dup).exists()


@pytest.mark.django_db
def test_tenantconnection_repoints_to_canonical_when_no_oauth_overlap():
    """Duplicate's connection (and its membership) move to canonical when
    canonical has no competing OAuth connection for that provider."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    tenant = Tenant.objects.create(provider="ocs", external_id="exp1", canonical_name="Bot")
    conn = TenantConnection.objects.create(
        user=duplicate,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
    )
    tm = TenantMembership.objects.create(user=duplicate, tenant=tenant, connection=conn)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.tenant_connection_repointed == 1
    assert report.tenant_connection_conflict_merged == 0
    conn.refresh_from_db()
    assert conn.user == canonical
    tm.refresh_from_db()
    assert tm.user == canonical
    assert tm.connection_id == conn.id


@pytest.mark.django_db
def test_tenantconnection_oauth_conflict_merges_into_canonical():
    """Both users hold an OCS OAuth connection. After merge canonical keeps
    exactly one OCS OAuth connection, the duplicate's is gone, and the
    duplicate's memberships now point at canonical's connection."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")

    canon_conn = TenantConnection.objects.create(
        user=canonical,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
    )
    dup_conn = TenantConnection.objects.create(
        user=duplicate,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
    )
    # A membership on the duplicate's connection that must be repointed.
    only_dup_tenant = Tenant.objects.create(
        provider="ocs", external_id="exp-dup", canonical_name="DupBot"
    )
    dup_tm = TenantMembership.objects.create(
        user=duplicate, tenant=only_dup_tenant, connection=dup_conn
    )

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.tenant_connection_conflict_merged == 1
    assert report.tenant_connection_repointed == 0
    # Canonical owns exactly one OCS OAuth connection — its original one.
    ocs_oauth = TenantConnection.objects.filter(
        user=canonical, provider="ocs", credential_type=TenantConnection.OAUTH
    )
    assert ocs_oauth.count() == 1
    assert ocs_oauth.get().id == canon_conn.id
    # The duplicate's connection is deleted.
    assert not TenantConnection.objects.filter(id=dup_conn.id).exists()
    # Its membership was repointed to canonical (user) and canonical's connection.
    dup_tm.refresh_from_db()
    assert dup_tm.user == canonical
    assert dup_tm.connection_id == canon_conn.id


@pytest.mark.django_db
def test_workspacemembership_repoints_when_no_overlap():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W")
    WorkspaceMembership.objects.create(workspace=ws, user=duplicate, role=WorkspaceRole.READ)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.workspace_membership_repointed == 1
    assert report.workspace_membership_conflict_merged == 0
    assert WorkspaceMembership.objects.get(workspace=ws).user == canonical


@pytest.mark.django_db
def test_workspacemembership_conflict_upgrades_to_higher_role():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W")
    WorkspaceMembership.objects.create(workspace=ws, user=canonical, role=WorkspaceRole.READ)
    WorkspaceMembership.objects.create(workspace=ws, user=duplicate, role=WorkspaceRole.MANAGE)

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.workspace_membership_conflict_merged == 1
    membership = WorkspaceMembership.objects.get(workspace=ws)
    assert membership.user == canonical
    assert membership.role == WorkspaceRole.MANAGE


@pytest.mark.django_db
def test_workspacemembership_conflict_keeps_canonical_when_higher():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W")
    WorkspaceMembership.objects.create(workspace=ws, user=canonical, role=WorkspaceRole.MANAGE)
    WorkspaceMembership.objects.create(workspace=ws, user=duplicate, role=WorkspaceRole.READ)

    merge_users(canonical=canonical, duplicate=duplicate)

    assert WorkspaceMembership.objects.get(workspace=ws).role == WorkspaceRole.MANAGE


@pytest.mark.django_db
def test_chat_threads_are_repointed_via_introspection():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    duplicate_pk = duplicate.pk
    ws = Workspace.objects.create(name="W")
    Thread.objects.create(user=duplicate, workspace=ws, title="T1")
    Thread.objects.create(user=duplicate, workspace=ws, title="T2")

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert Thread.objects.filter(user=canonical).count() == 2
    assert Thread.objects.filter(user_id=duplicate_pk).count() == 0
    label = "chat.Thread.user"
    assert report.long_tail_fk_counts.get(label) == 2


@pytest.mark.django_db
def test_setnull_fk_is_repointed_via_introspection():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    ws = Workspace.objects.create(name="W", created_by=duplicate)

    merge_users(canonical=canonical, duplicate=duplicate)

    ws.refresh_from_db()
    assert ws.created_by == canonical


@pytest.mark.django_db
def test_workspacemembership_invited_by_is_repointed_via_introspection():
    """invited_by is a SET_NULL FK to User; it should be repointed by the
    long-tail loop even though WorkspaceMembership.user has special-case
    handling for the role-merge path."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    other = User.objects.create(email="other@y.com", username="other")
    ws = Workspace.objects.create(name="W")
    # `other` has a membership; `duplicate` invited them.
    WorkspaceMembership.objects.create(
        workspace=ws,
        user=other,
        role=WorkspaceRole.READ,
        invited_by=duplicate,
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    membership = WorkspaceMembership.objects.get(workspace=ws, user=other)
    assert membership.invited_by == canonical


@pytest.mark.django_db
def test_duplicate_user_row_is_deleted():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")

    report = merge_users(canonical=canonical, duplicate=duplicate)

    assert report.duplicate_user_deleted is True
    assert not User.objects.filter(pk=duplicate.pk).exists()
    assert User.objects.filter(pk=canonical.pk).exists()


@pytest.mark.django_db
def test_merge_rolls_back_on_exception():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    SocialAccount.objects.create(user=duplicate, provider="commcare", uid="42")

    with (
        patch(
            "apps.users.services.merge._repoint_long_tail_fks",
            side_effect=RuntimeError("simulated failure"),
        ),
        pytest.raises(RuntimeError),
    ):
        merge_users(canonical=canonical, duplicate=duplicate)

    # Everything must be untouched
    assert User.objects.filter(pk=duplicate.pk).exists()
    assert SocialAccount.objects.get(provider="commcare", uid="42").user == duplicate


@pytest.mark.django_db
def test_dry_run_writes_nothing_and_returns_a_plan():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    SocialAccount.objects.create(user=duplicate, provider="commcare", uid="42")
    EmailAddress.objects.create(user=canonical, email="canon@y.com", primary=True)
    EmailAddress.objects.create(user=duplicate, email="canon@y.com", primary=True)

    report = merge_users(canonical=canonical, duplicate=duplicate, dry_run=True)

    assert report.dry_run is True
    assert report.socialaccount_repointed == 1
    assert report.emailaddress_deleted == 1
    # Nothing actually changed
    assert SocialAccount.objects.get(provider="commcare", uid="42").user == duplicate
    assert User.objects.filter(pk=duplicate.pk).exists()
    assert not report.duplicate_user_deleted


@pytest.mark.django_db
def test_merge_skips_auth_group_through_tables_when_shared():
    """Both users share a django.contrib.auth Group. The introspection loop
    must skip the User_groups through-table, otherwise the bulk update would
    IntegrityError on the (user, group) unique constraint."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    g = Group.objects.create(name="shared-group")
    canonical.groups.add(g)
    duplicate.groups.add(g)

    # Should not raise IntegrityError
    merge_users(canonical=canonical, duplicate=duplicate)

    # Canonical still has the group; through-table state is consistent.
    canonical.refresh_from_db()
    assert g in canonical.groups.all()


# ---------------------------------------------------------------------------
# 12#0 item 8: TenantMetadata fate across a merge
#
# TenantMetadata is a OneToOne on TenantMembership (on_delete=CASCADE), and
# TenantMembership.user is on_delete=CASCADE. The merge suite had ZERO
# TenantMetadata assertions, so the data-preservation behaviour of the
# membership repoint vs conflict-delete was invisible. Pin it: discovered
# provider metadata must ride along when a membership is repointed, and the
# canonical's own metadata must survive a conflict-delete of the duplicate's row.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_merge_preserves_tenant_metadata_when_membership_is_repointed():
    """A duplicate-only tenant: the membership is repointed (user=canonical), so
    its OneToOne TenantMetadata must survive and now be reachable via the
    canonical's membership — no discovered metadata is lost."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    only_dup = Tenant.objects.create(provider="ocs", external_id="exp1", canonical_name="Exp1")
    dup_tm = TenantMembership.objects.create(user=duplicate, tenant=only_dup)
    TenantMetadata.objects.create(
        tenant_membership=dup_tm,
        metadata={"team_slug": "alpha", "discovered": True},
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    # The membership row is the same row, now owned by canonical — so its
    # metadata is preserved (not cascade-deleted with the duplicate user).
    canon_tm = TenantMembership.objects.get(user=canonical, tenant=only_dup)
    md = TenantMetadata.objects.get(tenant_membership=canon_tm)
    assert md.metadata == {"team_slug": "alpha", "discovered": True}
    # And nothing was orphaned/dropped: exactly one metadata row still exists.
    assert TenantMetadata.objects.count() == 1


@pytest.mark.django_db
def test_merge_conflict_delete_keeps_canonical_tenant_metadata():
    """When both users belong to the same tenant, the duplicate's membership is
    conflict-deleted (its metadata cascades away with it), but the canonical
    keeps its OWN membership and metadata for that tenant — no canonical data is
    lost. Whichever side survives, the tenant stays covered."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    shared = Tenant.objects.create(provider="ocs", external_id="shared1", canonical_name="Shared")

    canon_tm = TenantMembership.objects.create(user=canonical, tenant=shared)
    TenantMetadata.objects.create(
        tenant_membership=canon_tm,
        metadata={"owner": "canonical"},
    )
    dup_tm = TenantMembership.objects.create(user=duplicate, tenant=shared)  # conflict
    TenantMetadata.objects.create(
        tenant_membership=dup_tm,
        metadata={"owner": "duplicate"},
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    # The duplicate's membership for the shared tenant is conflict-deleted, and
    # its OneToOne metadata cascades away with it. The canonical keeps its OWN
    # membership + metadata, so the tenant stays covered (no canonical data lost).
    surviving = TenantMembership.objects.get(user=canonical, tenant=shared)
    md = TenantMetadata.objects.get(tenant_membership=surviving)
    assert md.metadata == {"owner": "canonical"}
    # The duplicate's redundant metadata cascaded away with its membership — no
    # dangling rows pointing at the deleted membership remain.
    assert TenantMetadata.objects.count() == 1
    assert TenantMetadata.objects.filter(tenant_membership=dup_tm.id).count() == 0


@pytest.mark.django_db
def test_merge_conflict_migrates_duplicate_metadata_when_canonical_has_none():
    """04#1: both users belong to the same tenant, but only the DUPLICATE's
    membership carries discovered TenantMetadata. The conflict path must migrate
    that metadata onto the canonical's bare membership BEFORE deleting the
    duplicate's row — otherwise the only copy of the discovered metadata is
    cascade-deleted."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    shared = Tenant.objects.create(provider="ocs", external_id="shared1", canonical_name="Shared")

    # Canonical membership has NO metadata.
    canon_tm = TenantMembership.objects.create(user=canonical, tenant=shared)
    # Duplicate membership carries the only discovered metadata.
    dup_tm = TenantMembership.objects.create(user=duplicate, tenant=shared)
    TenantMetadata.objects.create(
        tenant_membership=dup_tm,
        metadata={"team_slug": "alpha", "discovered": True},
        discovered_at=None,
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    # The duplicate's membership is gone (conflict-deleted) but its metadata was
    # migrated onto the canonical's surviving membership — not cascade-deleted.
    surviving = TenantMembership.objects.get(user=canonical, tenant=shared)
    assert surviving.id == canon_tm.id
    md = TenantMetadata.objects.get(tenant_membership=surviving)
    assert md.metadata == {"team_slug": "alpha", "discovered": True}
    assert TenantMetadata.objects.count() == 1
    assert not TenantMembership.objects.filter(pk=dup_tm.pk).exists()


@pytest.mark.django_db
def test_merge_conflict_migrates_duplicate_connection_when_canonical_unwired():
    """04#1: on a tenant both users share, only the DUPLICATE's membership is
    wired to a connection. The conflict path must carry that connection wiring
    onto the canonical's unwired membership so it is not left connection=None."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    shared = Tenant.objects.create(provider="ocs", external_id="shared2", canonical_name="Shared2")

    # Canonical's membership is unwired (connection=None).
    canon_tm = TenantMembership.objects.create(user=canonical, tenant=shared, connection=None)
    # Duplicate's membership is wired to its OAuth connection.
    dup_conn = TenantConnection.objects.create(
        user=duplicate,
        provider="ocs",
        credential_type=TenantConnection.OAUTH,
    )
    TenantMembership.objects.create(user=duplicate, tenant=shared, connection=dup_conn)

    merge_users(canonical=canonical, duplicate=duplicate)

    surviving = TenantMembership.objects.get(user=canonical, tenant=shared)
    assert surviving.id == canon_tm.id
    # The connection moved to canonical (via _merge_tenant_connections) AND the
    # surviving membership is now wired to it — not left connection=None.
    surviving.refresh_from_db()
    assert surviving.connection_id is not None
    assert surviving.connection.user_id == canonical.pk


@pytest.mark.django_db
def test_merge_conflict_migrates_provider_metadata_when_canonical_empty():
    """04#1: the JSON provider_metadata (team_slug/team_name) on the duplicate's
    membership is preserved onto the canonical's membership when the canonical's
    is empty."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    shared = Tenant.objects.create(provider="ocs", external_id="shared3", canonical_name="Shared3")

    TenantMembership.objects.create(user=canonical, tenant=shared, provider_metadata={})
    TenantMembership.objects.create(
        user=duplicate,
        tenant=shared,
        provider_metadata={"team_slug": "beta", "team_name": "Beta Team"},
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    surviving = TenantMembership.objects.get(user=canonical, tenant=shared)
    assert surviving.provider_metadata == {"team_slug": "beta", "team_name": "Beta Team"}


@pytest.mark.django_db
def test_merge_conflict_does_not_clobber_canonical_metadata():
    """04#1 guardrail: when the canonical membership ALREADY has metadata, the
    duplicate's conflicting metadata is discarded (cascade) — the canonical's is
    never overwritten."""
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(email="dup@y.com", username="dup")
    shared = Tenant.objects.create(provider="ocs", external_id="shared4", canonical_name="Shared4")

    canon_tm = TenantMembership.objects.create(
        user=canonical, tenant=shared, provider_metadata={"team_slug": "canon"}
    )
    TenantMetadata.objects.create(tenant_membership=canon_tm, metadata={"owner": "canonical"})
    dup_tm = TenantMembership.objects.create(
        user=duplicate, tenant=shared, provider_metadata={"team_slug": "dup"}
    )
    TenantMetadata.objects.create(tenant_membership=dup_tm, metadata={"owner": "duplicate"})

    merge_users(canonical=canonical, duplicate=duplicate)

    surviving = TenantMembership.objects.get(user=canonical, tenant=shared)
    assert surviving.provider_metadata == {"team_slug": "canon"}
    md = TenantMetadata.objects.get(tenant_membership=surviving)
    assert md.metadata == {"owner": "canonical"}
    assert TenantMetadata.objects.count() == 1
