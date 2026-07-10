"""The authorizer must distinguish *why* access is denied so a member who lost
upstream (tenant) access sees an actionable message instead of a dead 403."""

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.access import (
    NOT_MEMBER,
    TENANT_ACCESS_LOST,
    access_denied_body,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant

User = get_user_model()


@pytest.mark.django_db
def test_non_member_gets_generic_denial():
    user = User.objects.create_user(email="denial-nonmember@example.com", password="pass")

    result = resolve_workspace_access_ex(user, uuid.uuid4())

    assert not result.granted
    assert result.denied_reason == NOT_MEMBER
    assert result.lost_tenant_names == ()
    body = access_denied_body(result)
    assert body == {"error": "Workspace not found or access denied."}
    assert "reason" not in body


@pytest.mark.django_db
def test_member_without_live_tenant_gets_tenant_access_lost():
    user = User.objects.create_user(email="denial-lost@example.com", password="pass")
    tenant = Tenant.objects.create(
        provider="commcare", external_id="skelly", canonical_name="skelly"
    )
    ws = Workspace.objects.create(name="Skelly WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    # No live TenantMembership: upstream access was removed.

    result = resolve_workspace_access_ex(user, ws.id)

    assert not result.granted
    assert result.denied_reason == TENANT_ACCESS_LOST
    assert result.lost_tenant_names == ("skelly",)
    body = access_denied_body(result)
    assert body["reason"] == TENANT_ACCESS_LOST
    assert body["lost_tenants"] == ["skelly"]
    assert "skelly" in body["error"]


@pytest.mark.django_db
def test_member_with_live_tenant_is_granted():
    user = User.objects.create_user(email="denial-ok@example.com", password="pass")
    tenant = Tenant.objects.create(
        provider="commcare", external_id="live", canonical_name="Live"
    )
    ws = Workspace.objects.create(name="Live WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    TenantMembership.objects.create(user=user, tenant=tenant)

    result = resolve_workspace_access_ex(user, ws.id)

    assert result.granted
    assert result.workspace == ws
    assert result.denied_reason is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_member_without_live_tenant_names_lost_projects():
    user = await User.objects.acreate_user(email="denial-async@example.com", password="pass")
    t1 = await Tenant.objects.acreate(
        provider="commcare", external_id="skelly", canonical_name="skelly"
    )
    t2 = await Tenant.objects.acreate(
        provider="commcare", external_id="bones", canonical_name="bones"
    )
    ws = await Workspace.objects.acreate(name="Multi WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)

    result = await aresolve_workspace_access_ex(user, ws.id)

    assert not result.granted
    assert result.denied_reason == TENANT_ACCESS_LOST
    assert result.lost_tenant_names == ("bones", "skelly")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_non_member_generic():
    user = await User.objects.acreate_user(email="denial-async-nm@example.com", password="pass")

    result = await aresolve_workspace_access_ex(user, uuid.uuid4())

    assert result.denied_reason == NOT_MEMBER
    assert access_denied_body(result) == {"error": "Workspace not found or access denied."}
