import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.users.models import Tenant, TenantMembership

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_single_tenant_workspace_returns_membership():
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
    assert tm.tenant.external_id == "single-domain"


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_multi_tenant_workspace_returns_none_membership():
    """Multi-tenant workspaces must return None for tenant_membership so routing uses workspace_id."""
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
    await TenantMembership.objects.acreate(user=user, tenant=t1)
    await TenantMembership.objects.acreate(user=user, tenant=t2)

    workspace, tm = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert tm is None  # critical: must be None even though user has TenantMembership for both
