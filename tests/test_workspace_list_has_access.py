"""GET /api/workspaces/ annotates each workspace with live `has_access`.

The list returns every membership (so orphaned workspaces stay addressable by
URL), but flags the ones the user has lost upstream tenant access to — the same
rule apps/workspaces/access.py gates each request with.
"""

import pytest
from django.test import Client
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


def _entry(resp, workspace_id):
    return next(w for w in resp.json() if w["id"] == str(workspace_id))


@pytest.mark.django_db
def test_member_with_live_tenant_has_access(client, user, workspace):
    client.force_login(user)
    resp = client.get("/api/workspaces/")
    assert resp.status_code == 200
    assert _entry(resp, workspace.id)["has_access"] is True


@pytest.mark.django_db
def test_member_without_live_tenant_lost_access(client, user):
    tenant = Tenant.objects.create(
        provider="commcare", external_id="skelly", canonical_name="skelly"
    )
    ws = Workspace.objects.create(name="Skelly WS", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    # Member, but no live TenantMembership — upstream access was removed.

    client.force_login(user)
    resp = client.get("/api/workspaces/")

    entry = _entry(resp, ws.id)
    assert entry["has_access"] is False
    assert entry["tenants"][0]["provider"] == "commcare"


@pytest.mark.django_db
def test_zero_tenant_workspace_has_access(client, user):
    ws = Workspace.objects.create(name="Tenantless", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)

    client.force_login(user)
    resp = client.get("/api/workspaces/")

    assert _entry(resp, ws.id)["has_access"] is True


@pytest.mark.django_db
def test_archived_tenant_membership_lost_access(client, user):
    """An archived (revoked) TenantMembership must not count as live access."""
    tenant = Tenant.objects.create(
        provider="commcare", external_id="revoked", canonical_name="Revoked Domain"
    )
    ws = Workspace.objects.create(name="Revoked WS", created_by=user)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    TenantMembership.all_objects.create(
        user=user, tenant=tenant, archived_at=timezone.now()
    )

    client.force_login(user)
    resp = client.get("/api/workspaces/")

    assert _entry(resp, ws.id)["has_access"] is False
