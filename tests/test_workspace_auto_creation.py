"""Tests for auto-create workspace on TenantMembership creation (Task 2.2)."""

import pytest

from apps.users.models import TenantMembership
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole


@pytest.mark.django_db
def test_workspace_auto_created_on_tenant_membership_creation(user, tenant):
    TenantMembership.objects.create(user=user, tenant=tenant)
    ws = Workspace.objects.filter(
        is_auto_created=True,
        memberships__user=user,
        workspace_tenants__tenant=tenant,
    ).first()
    assert ws is not None
    assert ws.name == tenant.canonical_name


@pytest.mark.django_db
def test_auto_created_workspace_gives_user_manage_role(user, tenant):
    TenantMembership.objects.create(user=user, tenant=tenant)
    membership = WorkspaceMembership.objects.get(
        workspace__is_auto_created=True,
        workspace__workspace_tenants__tenant=tenant,
        user=user,
    )
    assert membership.role == WorkspaceRole.MANAGE


@pytest.mark.django_db
def test_auto_creation_is_idempotent(user, tenant):
    TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    count = Workspace.objects.filter(
        is_auto_created=True,
        memberships__user=user,
        workspace_tenants__tenant=tenant,
    ).count()
    assert count == 1


@pytest.mark.django_db
def test_updating_tenant_membership_does_not_create_duplicate_workspace(user, tenant):
    tm = TenantMembership.objects.create(user=user, tenant=tenant)
    tm.last_selected_at = None
    tm.save()  # triggers post_save with created=False
    count = Workspace.objects.filter(
        is_auto_created=True,
        workspace_tenants__tenant=tenant,
    ).count()
    assert count == 1
