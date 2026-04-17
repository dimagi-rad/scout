"""Tests for Workspace, WorkspaceTenant, WorkspaceMembership models (Task 2.1)."""

import pytest
from django.db.utils import IntegrityError

from apps.users.models import Tenant
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant


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


@pytest.mark.django_db
def test_display_name_for_connect_workspace_includes_opp_id(user):
    """Connect provider template renders "{name} (Opp {external_id})"."""
    connect_tenant = Tenant.objects.create(
        provider="commcare_connect",
        external_id="opp-42",
        canonical_name="Malaria Campaign",
    )
    ws = Workspace.objects.create(name="Malaria Campaign", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=connect_tenant)

    assert ws.display_name == "Malaria Campaign (Opp opp-42)"


@pytest.mark.django_db
def test_display_name_for_commcare_workspace_is_plain_name(workspace):
    """CommCare provider template is just "{name}" — no added context."""
    assert workspace.display_name == workspace.name


@pytest.mark.django_db
def test_display_name_without_tenant_falls_back_to_name(user):
    """A workspace with no tenants returns its raw name."""
    ws = Workspace.objects.create(name="Tenantless", created_by=user)
    assert ws.display_name == "Tenantless"


@pytest.mark.django_db
def test_display_name_with_unknown_provider_falls_back_to_name(user):
    """A provider without a template entry falls back to the raw name."""
    # Bypass choices validation to simulate a provider with no template entry.
    tenant = Tenant(provider="future-provider", external_id="abc", canonical_name="Future")
    tenant.save()
    ws = Workspace.objects.create(name="Mystery", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)

    assert ws.display_name == "Mystery"
