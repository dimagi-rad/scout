import pytest

from apps.workspaces.models import SchemaState, WorkspaceTenant, WorkspaceViewSchema


@pytest.fixture
def tenant2(db):
    from apps.users.models import Tenant

    return Tenant.objects.create(
        provider="commcare", external_id="test-domain-2", canonical_name="Test Domain 2"
    )


@pytest.fixture
def tenant_membership2(db, user, tenant2):
    from apps.users.models import TenantMembership

    return TenantMembership.objects.create(user=user, tenant=tenant2)


@pytest.mark.django_db
def test_add_workspace_tenant_creates_record_and_marks_provisioning(
    workspace, tenant2, tenant_membership2
):
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_test", state=SchemaState.ACTIVE
    )
    from apps.workspaces.services.workspace_service import add_workspace_tenant

    add_workspace_tenant(workspace, tenant2)

    assert WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant2).exists()
    vs.refresh_from_db()
    assert vs.state == SchemaState.PROVISIONING


@pytest.mark.django_db
def test_remove_workspace_tenant_deletes_record_and_marks_provisioning(
    workspace, tenant2, tenant_membership2
):
    wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_test", state=SchemaState.ACTIVE
    )
    from apps.workspaces.services.workspace_service import remove_workspace_tenant

    remove_workspace_tenant(workspace, wt)

    assert not WorkspaceTenant.objects.filter(id=wt.id).exists()
    vs.refresh_from_db()
    assert vs.state == SchemaState.PROVISIONING
