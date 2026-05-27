from unittest.mock import patch

import pytest

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import SchemaState, WorkspaceTenant, WorkspaceViewSchema
from apps.workspaces.services.workspace_service import (
    add_workspace_tenant,
    remove_workspace_tenant,
)


@pytest.fixture
def tenant2(db):
    return Tenant.objects.create(
        provider="commcare", external_id="test-domain-2", canonical_name="Test Domain 2"
    )


@pytest.fixture
def tenant_membership2(db, user, tenant2):
    return TenantMembership.objects.create(user=user, tenant=tenant2)


@pytest.fixture
def tenant3(db):
    return Tenant.objects.create(
        provider="commcare", external_id="test-domain-3", canonical_name="Test Domain 3"
    )


@pytest.fixture
def tenant_membership3(db, user, tenant3):
    return TenantMembership.objects.create(user=user, tenant=tenant3)


@pytest.mark.django_db
def test_add_workspace_tenant_creates_record_and_marks_provisioning(
    workspace, tenant2, tenant_membership2
):
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_test", state=SchemaState.ACTIVE
    )

    add_workspace_tenant(workspace, tenant2)

    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant2).exists()
    vs.refresh_from_db()
    assert vs.state == SchemaState.PROVISIONING


@pytest.mark.django_db
def test_remove_tenant_dispatches_view_schema_teardown_when_count_drops_to_one(
    workspace, tenant2, tenant_membership2
):
    wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_test", state=SchemaState.ACTIVE
    )

    with (
        patch(
            "apps.workspaces.services.workspace_service.teardown_view_schema_task.defer"
        ) as mock_teardown,
        patch(
            "apps.workspaces.services.workspace_service.rebuild_workspace_view_schema.defer"
        ) as mock_rebuild,
    ):
        remove_workspace_tenant(workspace, wt)

    assert not WorkspaceTenant.objects.filter(id=wt.id).exists()
    vs.refresh_from_db()
    assert vs.state == SchemaState.TEARDOWN
    mock_teardown.assert_called_once_with(view_schema_id=str(vs.id))
    mock_rebuild.assert_not_called()


@pytest.mark.django_db
def test_remove_tenant_no_op_on_tenant_count_above_one(
    workspace, tenant2, tenant_membership2, tenant3, tenant_membership3
):
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    wt3 = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant3)
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_test", state=SchemaState.ACTIVE
    )

    with (
        patch(
            "apps.workspaces.services.workspace_service.teardown_view_schema_task.defer"
        ) as mock_teardown,
        patch(
            "apps.workspaces.services.workspace_service.rebuild_workspace_view_schema.defer"
        ) as mock_rebuild,
    ):
        remove_workspace_tenant(workspace, wt3)

    assert not WorkspaceTenant.objects.filter(id=wt3.id).exists()
    vs.refresh_from_db()
    assert vs.state == SchemaState.PROVISIONING
    mock_rebuild.assert_called_once_with(workspace_id=str(workspace.id))
    mock_teardown.assert_not_called()
