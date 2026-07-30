import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_single_tenant_workspace_with_membership_is_accessible():
    """Single-tenant workspace returns TenantMembership when user holds one."""
    from apps.chat.helpers import _resolve_workspace_and_membership

    user = await User.objects.acreate_user(email="resolve-single@example.com", password="pass")
    t = await Tenant.objects.acreate(
        provider="commcare", external_id="single-domain", canonical_name="Single"
    )
    ws = await Workspace.objects.acreate(name="Single WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t)
    await TenantMembership.objects.acreate(user=user, tenant=t)

    workspace, tm, _is_multi_tenant = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert tm is not None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_single_tenant_workspace_without_membership_is_inaccessible():
    """No live TenantMembership ⇒ no access at all (authorizer denies the workspace)."""
    from apps.chat.helpers import _resolve_workspace_and_membership

    user = await User.objects.acreate_user(email="resolve-nomem@example.com", password="pass")
    t = await Tenant.objects.acreate(
        provider="commcare", external_id="nomem-domain", canonical_name="NoMem"
    )
    ws = await Workspace.objects.acreate(name="NoMem WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t)
    # no TenantMembership created

    workspace, tm, _is_multi_tenant = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is None  # WorkspaceMembership alone no longer grants access
    assert tm is None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_multi_tenant_workspace_without_live_tenant_is_inaccessible():
    """Closes the old hole: a multi-tenant workspace no longer grants access on
    WorkspaceMembership alone — the user must share at least one live tenant."""
    from apps.chat.helpers import _resolve_workspace_and_membership

    user = await User.objects.acreate_user(email="resolve-multi@example.com", password="pass")
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
    # no TenantMembership for either tenant

    workspace, tm, _is_multi_tenant = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is None  # multi-tenant hole closed
    assert tm is None


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_multi_tenant_workspace_returns_none_tm_even_with_tenant_membership():
    """Multi-tenant workspaces always return tm=None even if the user has a TenantMembership."""
    from apps.chat.helpers import _resolve_workspace_and_membership

    user = await User.objects.acreate_user(email="resolve-multi-tm@example.com", password="pass")
    t1 = await Tenant.objects.acreate(
        provider="commcare", external_id="mt2-domain-1", canonical_name="MT2-T1"
    )
    t2 = await Tenant.objects.acreate(
        provider="commcare", external_id="mt2-domain-2", canonical_name="MT2-T2"
    )
    ws = await Workspace.objects.acreate(name="Multi WS2", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)
    # User has TenantMembership for t1 (first tenant) — must still get tm=None
    await TenantMembership.objects.acreate(user=user, tenant=t1)

    workspace, tm, is_multi_tenant = await _resolve_workspace_and_membership(user, ws.id)
    assert workspace is not None
    assert tm is None
    assert is_multi_tenant is True


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_workspace_not_found_returns_none():
    """Returns (None, None) when the workspace doesn't exist or user lacks WorkspaceMembership."""
    from apps.chat.helpers import _resolve_workspace_and_membership

    user = await User.objects.acreate_user(email="resolve-missing@example.com", password="pass")

    workspace, tm, _is_multi_tenant = await _resolve_workspace_and_membership(user, uuid.uuid4())
    assert workspace is None
    assert tm is None
