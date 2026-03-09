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
    """Single-tenant workspace is accessible when user holds TenantMembership."""
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

    workspace, is_accessible = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert is_accessible is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_single_tenant_workspace_without_membership_is_inaccessible():
    """Single-tenant workspace is inaccessible when user lacks TenantMembership."""
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

    workspace, is_accessible = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert is_accessible is False


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_multi_tenant_workspace_is_accessible():
    """Multi-tenant workspaces are accessible via workspace_id routing."""
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

    workspace, is_accessible = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert is_accessible is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_workspace_not_found_returns_none():
    """Returns (None, False) when the workspace doesn't exist or user lacks WorkspaceMembership."""
    from apps.chat.views import _resolve_workspace_and_membership

    user = await sync_to_async(User.objects.create_user)(
        email="resolve-missing@example.com", password="pass"
    )

    workspace, is_accessible = await _resolve_workspace_and_membership(user, uuid.uuid4())
    assert workspace is None
    assert is_accessible is False
