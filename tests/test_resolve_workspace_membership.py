import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.users.models import Tenant, TenantMembership

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_single_tenant_workspace_with_membership_is_accessible():
    """Single-tenant workspace returns TenantMembership when user holds one."""
    from apps.chat.views import _resolve_workspace_and_membership

    user = await sync_to_async(User.objects.create_user)(
        email="resolve-single@example.com", password="pass"
    )
    t = await Tenant.objects.acreate(
        provider="commcare", external_id="single-domain", canonical_name="Single"
    )
    ws = await Workspace.objects.acreate(name="Single WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t)
    await TenantMembership.objects.acreate(user=user, tenant=t)

    workspace, tm = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert tm is not None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_single_tenant_workspace_without_membership_is_inaccessible():
    """Single-tenant workspace returns None tm when user lacks TenantMembership."""
    from apps.chat.views import _resolve_workspace_and_membership

    user = await sync_to_async(User.objects.create_user)(
        email="resolve-nomem@example.com", password="pass"
    )
    t = await Tenant.objects.acreate(
        provider="commcare", external_id="nomem-domain", canonical_name="NoMem"
    )
    ws = await Workspace.objects.acreate(name="NoMem WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t)
    # no TenantMembership created

    workspace, tm = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert tm is None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_multi_tenant_workspace_is_accessible():
    """Multi-tenant workspaces return None tm (caller re-checks count for access)."""
    from apps.chat.views import _resolve_workspace_and_membership

    user = await sync_to_async(User.objects.create_user)(
        email="resolve-multi@example.com", password="pass"
    )
    t1 = await Tenant.objects.acreate(
        provider="commcare", external_id="mt-domain-1", canonical_name="MT1"
    )
    t2 = await Tenant.objects.acreate(
        provider="commcare", external_id="mt-domain-2", canonical_name="MT2"
    )
    ws = await Workspace.objects.acreate(name="Multi WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)

    workspace, tm = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    # tm is None for multi-tenant (no single tenant to check); count > 1 grants access
    assert tm is None
    assert await workspace.workspace_tenants.acount() > 1


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_workspace_not_found_returns_none():
    """Returns (None, None) when the workspace doesn't exist or user lacks WorkspaceMembership."""
    from apps.chat.views import _resolve_workspace_and_membership

    user = await sync_to_async(User.objects.create_user)(
        email="resolve-missing@example.com", password="pass"
    )

    workspace, tm = await _resolve_workspace_and_membership(user, uuid.uuid4())
    assert workspace is None
    assert tm is None
