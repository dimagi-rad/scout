"""The authorizer must distinguish *why* access is denied so a member who lost
upstream (tenant) access sees an actionable message instead of a dead 403."""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.common.error_codes import ErrorCode
from apps.users.models import Tenant, TenantConnection, TenantMembership
from apps.workspaces.access import (
    NOT_MEMBER,
    TENANT_ACCESS_LOST,
    access_denied_body,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from apps.workspaces.services.failure_guidance import CREDENTIAL_GUIDANCE
from tests.tenant_access import grant_tenant_access

User = get_user_model()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("membership_role", "minimum_role", "granted"),
    [
        (WorkspaceRole.READ, WorkspaceRole.READ, True),
        (WorkspaceRole.READ, WorkspaceRole.READ_WRITE, False),
        (WorkspaceRole.READ, WorkspaceRole.MANAGE, False),
        (WorkspaceRole.READ_WRITE, WorkspaceRole.READ, True),
        (WorkspaceRole.READ_WRITE, WorkspaceRole.READ_WRITE, True),
        (WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE, False),
        (WorkspaceRole.MANAGE, WorkspaceRole.READ, True),
        (WorkspaceRole.MANAGE, WorkspaceRole.READ_WRITE, True),
        (WorkspaceRole.MANAGE, WorkspaceRole.MANAGE, True),
        ("unknown", WorkspaceRole.READ, False),
        (WorkspaceRole.MANAGE, "unknown", False),
    ],
)
def test_authorizer_enforces_ordered_minimum_role(
    user, workspace, membership_role, minimum_role, granted
):
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=user)
    membership.role = membership_role
    membership.save(update_fields=["role"])

    result = resolve_workspace_access_ex(user, workspace.id, minimum_role=minimum_role)

    assert result.granted is granted
    assert result.denied_reason == (None if granted else "insufficient_role")


@pytest.mark.django_db
def test_authorizer_defaults_to_read(user, workspace):
    membership = WorkspaceMembership.objects.get(workspace=workspace, user=user)
    membership.role = WorkspaceRole.READ
    membership.save(update_fields=["role"])

    result = resolve_workspace_access_ex(user, workspace.id)

    assert result.granted


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("membership_role", "minimum_role", "granted"),
    [
        (WorkspaceRole.READ, WorkspaceRole.READ, True),
        (WorkspaceRole.READ, WorkspaceRole.READ_WRITE, False),
        (WorkspaceRole.READ_WRITE, WorkspaceRole.READ_WRITE, True),
        (WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE, False),
        (WorkspaceRole.MANAGE, WorkspaceRole.MANAGE, True),
    ],
)
async def test_async_authorizer_enforces_ordered_minimum_role(
    membership_role, minimum_role, granted
):
    user = await User.objects.acreate_user(
        email=f"async-{membership_role}-{minimum_role}@example.com", password="pass"
    )
    workspace = await Workspace.objects.acreate(name="Async role", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=workspace, user=user, role=membership_role)

    result = await aresolve_workspace_access_ex(user, workspace.id, minimum_role=minimum_role)

    assert result.granted is granted
    assert result.denied_reason == (None if granted else "insufficient_role")


@pytest.mark.django_db
def test_non_member_gets_generic_denial():
    user = User.objects.create_user(email="denial-nonmember@example.com", password="pass")

    result = resolve_workspace_access_ex(user, uuid.uuid4(), minimum_role=WorkspaceRole.MANAGE)

    assert not result.granted
    assert result.denied_reason == NOT_MEMBER
    assert result.lost_tenant_names == ()
    body = access_denied_body(result)
    assert body == {"error": "Workspace not found or access denied."}
    assert "reason" not in body


@pytest.mark.django_db
@pytest.mark.parametrize("cause", ["not_connected", "disconnected", "upstream_denial"])
def test_member_without_live_tenant_gets_tenant_access_lost(cause):
    user = User.objects.create_user(email="denial-lost@example.com", password="pass")
    tenant = Tenant.objects.create(
        provider="commcare", external_id="skelly", canonical_name="skelly"
    )
    ws = Workspace.objects.create(name="Skelly WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.READ)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    if cause != "not_connected":
        connection = TenantConnection.objects.create(
            user=user,
            provider="commcare",
            credential_type=TenantConnection.API_KEY,
            upstream_denial_code=ErrorCode.AUTH_ACCESS_DENIED if cause == "upstream_denial" else "",
        )
        TenantMembership.all_objects.create(
            user=user, tenant=tenant, connection=connection, archived_at=timezone.now()
        )
        if cause == "disconnected":
            connection.delete()

    result = resolve_workspace_access_ex(user, ws.id, minimum_role=WorkspaceRole.MANAGE)

    assert not result.granted
    assert result.denied_reason == TENANT_ACCESS_LOST
    assert result.lost_tenant_names == ("skelly",)
    body = access_denied_body(result)
    assert body["reason"] == TENANT_ACCESS_LOST
    assert CREDENTIAL_GUIDANCE[ErrorCode.WORKSPACE_TENANT_UNREACHABLE] in body["error"]
    assert "if you disconnected it" in body["error"]
    assert "If access was removed or restricted at the provider" in body["error"]
    assert "reconnecting alone cannot restore those permissions" in body["error"]
    assert "A workspace admin can help remove" in body["error"]
    assert body["lost_tenants"] == ["skelly"]
    assert "skelly" in body["error"]


@pytest.mark.django_db
def test_member_with_live_tenant_is_granted():
    user = User.objects.create_user(email="denial-ok@example.com", password="pass")
    tenant = Tenant.objects.create(provider="commcare", external_id="live", canonical_name="Live")
    ws = Workspace.objects.create(name="Live WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    grant_tenant_access(user, tenant)

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
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.READ)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)

    result = await aresolve_workspace_access_ex(user, ws.id, minimum_role=WorkspaceRole.MANAGE)

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
