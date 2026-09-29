from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.tenant_coverage import coverage_complete, coverage_warning
from apps.workspaces.services.workspace_service import (
    LastWorkspaceTenant,
    add_workspace_tenant,
    remove_workspace_tenant,
    touch_workspace_schemas,
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


def _entry(tenant):
    return {
        "tenant_id": str(tenant.id),
        "provider": tenant.provider,
        "external_id": tenant.external_id,
    }


def _add_unloaded(workspace, tenant, user):
    with (
        patch(
            "apps.workspaces.services.workspace_service.rebuild_workspace_view_schema.defer"
        ) as rebuild,
        patch(
            "apps.workspaces.services.workspace_service.materialize_workspace.defer"
        ) as materialize,
    ):
        add_workspace_tenant(workspace, tenant, actor_id=user.id)
    rebuild.assert_called_once_with(workspace_id=str(workspace.id))
    materialize.assert_called_once()


@pytest.mark.django_db
def test_adding_an_unloaded_source_reports_it_missing_before_the_rebuild_runs(
    workspace, tenant, user, tenant2, tenant_membership2, tenant3, tenant_membership3
):
    """B3: coverage said "complete" until the queued rebuild ran, so answers
    silently omitted the new source."""
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    for source in (tenant, tenant2):
        TenantSchema.objects.create(
            tenant=source, schema_name=f"live_{source.external_id}", state=SchemaState.ACTIVE
        )
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_test",
        state=SchemaState.ACTIVE,
        tenant_coverage={
            "included_tenants": [_entry(tenant), _entry(tenant2)],
            "excluded_tenants": [],
        },
    )

    _add_unloaded(workspace, tenant3, user)

    vs.refresh_from_db()
    # Still serving the loaded sources, but no longer claiming to cover them all.
    assert vs.state == SchemaState.ACTIVE
    assert vs.tenant_coverage == {
        "included_tenants": [_entry(tenant), _entry(tenant2)],
        "excluded_tenants": [_entry(tenant3)],
    }
    assert coverage_complete(vs.tenant_coverage) is False
    assert "test-domain-3" in coverage_warning(vs.tenant_coverage)


@pytest.mark.django_db
def test_adding_an_unloaded_source_to_a_legacy_view_reports_it_missing(
    workspace, tenant, user, tenant2, tenant_membership2, tenant3, tenant_membership3
):
    """A view built before coverage was recorded can only serve sources with an
    ACTIVE schema, so a linked source without one is named missing too."""
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    TenantSchema.objects.create(tenant=tenant, schema_name="live_one", state=SchemaState.ACTIVE)
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_test", state=SchemaState.ACTIVE
    )

    _add_unloaded(workspace, tenant3, user)

    vs.refresh_from_db()
    assert vs.tenant_coverage == {
        "included_tenants": [_entry(tenant)],
        "excluded_tenants": [_entry(tenant2), _entry(tenant3)],
    }


@pytest.mark.django_db
def test_adding_an_unloaded_source_leaves_unknown_coverage_unknown(
    workspace, tenant, user, tenant2, tenant_membership2, tenant3, tenant_membership3
):
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_test",
        state=SchemaState.ACTIVE,
        tenant_coverage={"included_tenants": "garbled"},
    )

    _add_unloaded(workspace, tenant3, user)

    vs.refresh_from_db()
    assert vs.tenant_coverage == {"included_tenants": "garbled"}
    assert coverage_complete(vs.tenant_coverage) is None


@pytest.mark.parametrize("retired", [SchemaState.TEARDOWN, SchemaState.EXPIRED])
@pytest.mark.django_db
def test_adding_an_unloaded_source_marks_a_retired_view_schema_wanted_again(
    workspace, tenant, user, tenant2, tenant_membership2, retired
):
    """The queued rebuild skips a retired row, so the add must say it wants the views
    back, as adding a loaded source already does."""
    TenantSchema.objects.create(tenant=tenant, schema_name="live_one", state=SchemaState.ACTIVE)
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name="ws_test", state=retired
    )

    _add_unloaded(workspace, tenant2, user)

    vs.refresh_from_db()
    assert vs.state == SchemaState.PROVISIONING


@pytest.mark.parametrize(
    "state", [SchemaState.ACTIVE, SchemaState.PROVISIONING, SchemaState.FAILED]
)
@pytest.mark.django_db
def test_remove_tenant_dispatches_view_schema_teardown_when_count_drops_to_one(
    workspace, tenant2, tenant_membership2, state
):
    wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    vs = WorkspaceViewSchema.objects.create(workspace=workspace, schema_name="ws_test", state=state)

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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_touch_multitenant_workspace_touches_constituent_tenant_schemas(
    workspace, tenant, tenant2, tenant_membership2
):
    """Multi-tenant chat activity must refresh the TTL on each constituent
    TenantSchema, not just the view schema — otherwise the tenant schemas
    expire and their DROP CASCADE destroys the views in the view schema."""
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant2)

    stale = timezone.now() - timedelta(days=20)
    ts1 = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="touch_tenant_1",
        state=SchemaState.ACTIVE,
        last_accessed_at=stale,
    )
    ts2 = await TenantSchema.objects.acreate(
        tenant=tenant2,
        schema_name="touch_tenant_2",
        state=SchemaState.ACTIVE,
        last_accessed_at=stale,
    )
    vs = await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="ws_touchtest12345",
        state=SchemaState.ACTIVE,
        last_accessed_at=stale,
    )

    before = timezone.now()
    await touch_workspace_schemas(workspace)

    await ts1.arefresh_from_db()
    await ts2.arefresh_from_db()
    await vs.arefresh_from_db()
    assert ts1.last_accessed_at >= before
    assert ts2.last_accessed_at >= before
    # The view schema is still touched as well.
    assert vs.last_accessed_at >= before


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_touch_multitenant_touches_tenant_schemas_without_view_schema(
    workspace, tenant, tenant2, tenant_membership2
):
    """The tenant schemas underpin everything, so they must be touched even when
    no WorkspaceViewSchema row exists yet (e.g. mid-provision)."""
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant2)

    stale = timezone.now() - timedelta(days=20)
    ts1 = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name="touch_noview_1",
        state=SchemaState.ACTIVE,
        last_accessed_at=stale,
    )
    ts2 = await TenantSchema.objects.acreate(
        tenant=tenant2,
        schema_name="touch_noview_2",
        state=SchemaState.ACTIVE,
        last_accessed_at=stale,
    )

    before = timezone.now()
    await touch_workspace_schemas(workspace)

    await ts1.arefresh_from_db()
    await ts2.arefresh_from_db()
    assert ts1.last_accessed_at >= before
    assert ts2.last_accessed_at >= before


@pytest.mark.django_db
def test_remove_last_tenant_raises_last_workspace_tenant(workspace, tenant):
    wt = WorkspaceTenant.objects.get(workspace=workspace, tenant=tenant)

    with pytest.raises(LastWorkspaceTenant):
        remove_workspace_tenant(workspace, wt)

    assert WorkspaceTenant.objects.filter(id=wt.id).exists()


@pytest.mark.django_db
def test_removal_that_lost_a_race_is_not_a_last_source_removal(
    workspace, tenant, tenant2, tenant_membership2
):
    """Two removals of the same source: the loser must not read the survivor as the last."""
    WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant2)
    stale = WorkspaceTenant.objects.get(workspace=workspace, tenant=tenant)
    WorkspaceTenant.objects.filter(id=stale.id).delete()

    with patch(
        "apps.workspaces.services.workspace_service.rebuild_workspace_view_schema.defer"
    ) as mock_rebuild:
        remove_workspace_tenant(workspace, stale)

    assert list(workspace.workspace_tenants.values_list("tenant_id", flat=True)) == [tenant2.id]
    mock_rebuild.assert_not_called()
