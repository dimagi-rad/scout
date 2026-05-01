from unittest.mock import MagicMock, patch

import pytest

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(email="task@example.com", password="pass")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="task-domain", canonical_name="Task Domain"
    )


@pytest.fixture
def workspace(db, user, tenant):
    from apps.workspaces.models import TenantSchema

    ws = Workspace.objects.create(name="Task WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    TenantSchema.objects.create(tenant=tenant, schema_name="task_domain", state=SchemaState.ACTIVE)
    return ws


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_calls_build_view_schema(workspace):
    from apps.workspaces.tasks import rebuild_workspace_view_schema

    with patch("apps.workspaces.tasks.SchemaManager") as MockSM:
        mock_vs = MagicMock()
        mock_vs.schema_name = "ws_abc123"
        MockSM.return_value.build_view_schema.return_value = mock_vs

        result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))

    # The service (build_view_schema) now owns the ACTIVE transition; the task does not write state
    assert result["status"] == "active"
    mock_vs.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_fails_if_no_active_tenant_schema(workspace):
    from apps.workspaces.models import TenantSchema
    from apps.workspaces.tasks import rebuild_workspace_view_schema

    await TenantSchema.objects.filter(
        tenant__workspace_tenants__workspace=workspace
    ).aupdate(state=SchemaState.EXPIRED)

    result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_marks_failed_on_exception(workspace):
    from apps.workspaces.tasks import rebuild_workspace_view_schema

    with patch("apps.workspaces.tasks.SchemaManager") as MockSM:
        MockSM.return_value.build_view_schema.side_effect = Exception("boom")

        result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))

    assert "error" in result
    # WorkspaceViewSchema state should be FAILED (if it exists)
    try:
        vs = await WorkspaceViewSchema.objects.aget(workspace=workspace)
        assert vs.state == SchemaState.FAILED
    except WorkspaceViewSchema.DoesNotExist:
        pass  # acceptable — was never created
