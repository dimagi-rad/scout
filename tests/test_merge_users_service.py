"""Unit tests for apps.users.services.merge.merge_users and helpers."""

from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model

from apps.chat.models import Thread
from apps.users.models import Tenant, TenantCredential, TenantMembership
from apps.users.services.merge import merge_users, select_canonical
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole

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
def test_field_level_merge_ors_staff_and_superuser_flags():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(
        email="dup@y.com", username="dup", is_staff=True, is_superuser=True,
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.is_staff is True
    assert canonical.is_superuser is True


@pytest.mark.django_db
def test_field_level_merge_fills_empty_name_fields_from_duplicate():
    canonical = User.objects.create(email="canon@y.com", username="canon")
    duplicate = User.objects.create(
        email="dup@y.com", username="dup",
        first_name="Brian", last_name="DeRenzi", avatar_url="https://x/y.png",
    )

    merge_users(canonical=canonical, duplicate=duplicate)

    canonical.refresh_from_db()
    assert canonical.first_name == "Brian"
    assert canonical.last_name == "DeRenzi"
    assert canonical.avatar_url == "https://x/y.png"


@pytest.mark.django_db
def test_field_level_merge_keeps_canonical_name_when_already_set():
    canonical = User.objects.create(
        email="canon@y.com", username="canon", first_name="Already", last_name="Set",
    )
    duplicate = User.objects.create(
        email="dup@y.com", username="dup", first_name="Newer", last_name="Name",
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
    EmailAddress.objects.create(user=duplicate, email="brian-old@y.com", primary=True, verified=True)

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
    TenantCredential.objects.create(
        tenant_membership=canon_tm, credential_type=TenantCredential.OAUTH,
    )
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
        workspace=ws, user=other, role=WorkspaceRole.READ, invited_by=duplicate,
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

    with patch(
        "apps.users.services.merge._repoint_long_tail_fks",
        side_effect=RuntimeError("simulated failure"),
    ), pytest.raises(RuntimeError):
        merge_users(canonical=canonical, duplicate=duplicate)

    # Everything must be untouched
    assert User.objects.filter(pk=duplicate.pk).exists()
    assert SocialAccount.objects.get(provider="commcare", uid="42").user == duplicate
