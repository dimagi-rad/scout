"""Tests for Workspace, WorkspaceTenant, WorkspaceMembership models (Task 2.1)."""

import pytest
from django.db.utils import IntegrityError

from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant


@pytest.mark.django_db
def test_workspace_has_name_and_tenants(tenant, user):
    ws = Workspace.objects.create(name="My workspace", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    assert ws.tenants.first() == tenant


@pytest.mark.django_db
def test_workspace_tenant_property_returns_first_tenant(workspace, tenant):
    assert workspace.tenant == tenant


@pytest.mark.django_db
def test_workspace_membership_enforces_unique_user_per_workspace(workspace, user):
    with pytest.raises(IntegrityError):
        WorkspaceMembership.objects.create(workspace=workspace, user=user, role=WorkspaceRole.READ)


@pytest.mark.django_db
def test_workspace_membership_roles_exist():
    assert WorkspaceRole.READ == "read"
    assert WorkspaceRole.READ_WRITE == "read_write"
    assert WorkspaceRole.MANAGE == "manage"


@pytest.mark.django_db
def test_workspace_str(workspace):
    assert str(workspace) == workspace.name


@pytest.mark.django_db
def test_workspace_tenant_str_uniqueness(workspace, tenant, user):
    ws2 = Workspace.objects.create(name="Other", created_by=user)
    with pytest.raises(IntegrityError):
        WorkspaceTenant.objects.create(workspace=ws2, tenant=tenant)
        WorkspaceTenant.objects.create(workspace=ws2, tenant=tenant)
