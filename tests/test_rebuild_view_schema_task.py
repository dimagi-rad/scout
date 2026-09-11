from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.semantic.services.cube_schema import CubeSchemaBuildError
from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.tasks import rebuild_workspace_view_schema


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(email="task@example.com", password="pass")


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="task-domain", canonical_name="Task Domain"
    )


@pytest.fixture
def workspace(db, user, tenant):
    ws = Workspace.objects.create(name="Task WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=tenant)
    TenantSchema.objects.create(tenant=tenant, schema_name="task_domain", state=SchemaState.ACTIVE)
    return ws


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_calls_build_view_schema(workspace):
    with patch("apps.workspaces.tasks.SchemaManager") as MockSM:
        mock_vs = MagicMock()
        mock_vs.schema_name = "ws_abc123"
        mock_vs.tenant_coverage = {
            "included_tenants": [{"tenant_id": "included"}],
            "excluded_tenants": [{"tenant_id": "excluded"}],
        }
        MockSM.return_value.build_view_schema.return_value = mock_vs

        result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))

    # The service (build_view_schema) now owns the ACTIVE transition; the task does not write state
    assert result["status"] == "active"
    assert result["tenant_coverage"] == mock_vs.tenant_coverage
    mock_vs.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_reports_coverage_when_cube_build_fails(workspace):
    coverage = {
        "included_tenants": [{"tenant_id": "included"}],
        "excluded_tenants": [{"tenant_id": "excluded"}],
    }
    with (
        patch("apps.workspaces.tasks.SchemaManager") as mock_schema_manager,
        patch(
            "apps.workspaces.tasks.build_and_promote_cube_schema",
            side_effect=CubeSchemaBuildError("invalid cube"),
        ),
    ):
        mock_vs = mock_schema_manager.return_value.build_view_schema.return_value
        mock_vs.schema_name = "ws_abc123"
        mock_vs.tenant_coverage = coverage

        result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))

    assert result["status"] == "active"
    assert result["tenant_coverage"] == coverage
    assert result["cube_schema"] == {"ok": False, "error": "invalid cube"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_fails_if_no_active_tenant_schema(workspace, tenant):
    await TenantSchema.objects.filter(tenant__workspace_tenants__workspace=workspace).aupdate(
        state=SchemaState.EXPIRED
    )

    result = await rebuild_workspace_view_schema(workspace_id=str(workspace.id))
    assert "error" in result
    assert result["tenant_coverage"] == {
        "included_tenants": [],
        "excluded_tenants": [
            {
                "tenant_id": str(tenant.id),
                "provider": tenant.provider,
                "external_id": tenant.external_id,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_rebuild_view_schema_marks_failed_on_exception(workspace):
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
