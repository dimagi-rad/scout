"""Tests for schema TTL tasks."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.tasks import expire_inactive_schemas, teardown_schema


@pytest.fixture
def active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="ttl_test_schema",
        state=SchemaState.ACTIVE,
        last_accessed_at=timezone.now(),
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expire_inactive_schemas_marks_stale_schema_for_teardown(active_schema):
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=25)
    await active_schema.asave(update_fields=["last_accessed_at"])

    with patch(
        "apps.workspaces.tasks.teardown_schema.defer_async", new_callable=AsyncMock
    ) as mock_defer:
        from apps.workspaces.tasks import expire_inactive_schemas

        await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN
    mock_defer.assert_called_once_with(schema_id=str(active_schema.id))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resurrected_schema_survives_immediate_expire_sweep(tenant):
    """Regression for the production drop loop: an EXPIRED schema resurrected via
    provision() must NOT be re-expired by the very next expire_inactive_schemas
    tick. provision() now refreshes last_accessed_at, so the schema is fresh."""
    mgr = SchemaManager()
    schema_name = mgr._sanitize_schema_name(tenant.external_id)
    stale = timezone.now() - timedelta(days=20)
    await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name=schema_name,
        state=SchemaState.EXPIRED,
        last_accessed_at=stale,
    )

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = MagicMock()
    mock_conn.cursor.return_value.fetchone.return_value = None

    with patch(
        "apps.workspaces.services.schema_manager.get_managed_db_connection",
        return_value=mock_conn,
    ):
        ts = await sync_to_async(mgr.provision)(tenant)

    # Now run the janitor immediately, as production did 60s after materialization.
    with patch("apps.workspaces.tasks.teardown_schema.defer_async", new_callable=AsyncMock):
        await expire_inactive_schemas()

    await ts.arefresh_from_db()
    # The schema survives — it is still ACTIVE, not flipped to TEARDOWN.
    assert ts.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_active_schema_not_expired_if_recently_accessed(active_schema):
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=1)
    await active_schema.asave(update_fields=["last_accessed_at"])

    from apps.workspaces.tasks import expire_inactive_schemas

    await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_schema_with_null_last_accessed_is_not_expired(active_schema):
    """Schemas that have never been accessed (null) should not be auto-expired."""
    active_schema.last_accessed_at = None
    await active_schema.asave(update_fields=["last_accessed_at"])

    from apps.workspaces.tasks import expire_inactive_schemas

    await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_marks_expired_on_success(active_schema):
    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.return_value = None
        from apps.workspaces.tasks import teardown_schema

        await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.EXPIRED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expire_inactive_schemas_does_not_stale_runs_before_drop(active_schema):
    """expire_inactive_schemas only flips the schema to TEARDOWN and dispatches
    teardown_schema. It must NOT touch the data-bearing runs: the STALE flip is
    deferred to teardown_schema, which runs it only after the physical DROP
    succeeds. A schema in TEARDOWN is already unreachable via the catalog, so
    leaving the runs terminal here is both harmless and necessary (it lets the
    catalog recover the data intact if teardown later fails and reverts ACTIVE).
    """
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=25)
    await active_schema.asave(update_fields=["last_accessed_at"])

    completed_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {"cases": {"state": "completed", "rows": 1}}},
    )

    with patch("apps.workspaces.tasks.teardown_schema.defer_async", new_callable=AsyncMock):
        from apps.workspaces.tasks import expire_inactive_schemas

        await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    await completed_run.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN
    # The run is still terminal — NOT staled until the DROP actually happens.
    assert completed_run.state == MaterializationRun.RunState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_marks_runs_stale_on_success(active_schema):
    """After the physical DROP succeeds, the data-bearing runs (COMPLETED/PARTIAL)
    must be flipped to STALE so the catalog stops returning ghost entries for
    tables that no longer exist. CANCELLED/FAILED runs are left alone.
    """
    completed_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {"cases": {"state": "completed", "rows": 1}}},
    )
    partial_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.PARTIAL,
        result={"sources": {"cases": {"state": "completed", "rows": 1}}},
    )
    failed_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.FAILED,
    )

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.return_value = None
        from apps.workspaces.tasks import teardown_schema

        await teardown_schema(schema_id=str(active_schema.id))

    await completed_run.arefresh_from_db()
    await partial_run.arefresh_from_db()
    await failed_run.arefresh_from_db()

    assert completed_run.state == MaterializationRun.RunState.STALE
    assert partial_run.state == MaterializationRun.RunState.STALE
    assert failed_run.state == MaterializationRun.RunState.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_rolls_back_to_active_on_failure(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.side_effect = RuntimeError("DB error")
        from apps.workspaces.tasks import teardown_schema

        with pytest.raises(RuntimeError):
            await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_leaves_runs_terminal_when_drop_fails(active_schema):
    """Regression: a failed DROP must NOT leave the data-bearing runs STALE.

    The physical schema is still present and the record reverts to ACTIVE, so
    the runs must remain COMPLETED/PARTIAL — otherwise pipeline_list_tables
    (which filters runs to COMPLETED/PARTIAL) returns [] for an ACTIVE schema
    whose data is fully intact, and the user sees an empty workspace.
    """
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    completed_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {"cases": {"state": "completed", "rows": 1}}},
    )
    partial_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.PARTIAL,
        result={"sources": {"cases": {"state": "completed", "rows": 1}}},
    )

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.side_effect = RuntimeError("DB error")
        from apps.workspaces.tasks import teardown_schema

        with pytest.raises(RuntimeError):
            await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    await completed_run.arefresh_from_db()
    await partial_run.arefresh_from_db()

    # Schema is back to ACTIVE and the runs are still surfaced by the catalog.
    assert active_schema.state == SchemaState.ACTIVE
    assert completed_run.state == MaterializationRun.RunState.COMPLETED
    assert partial_run.state == MaterializationRun.RunState.PARTIAL


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_expire_then_failed_teardown_keeps_data_visible(active_schema):
    """Regression for the original defect: run the full expire -> teardown flow
    and let the DROP fail. The schema must revert to ACTIVE with its data-bearing
    runs still terminal, so the catalog (which filters runs to COMPLETED/PARTIAL)
    keeps surfacing the intact data rather than returning an empty workspace.
    """
    active_schema.last_accessed_at = timezone.now() - timedelta(hours=25)
    await active_schema.asave(update_fields=["last_accessed_at"])

    completed_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {"cases": {"state": "completed", "rows": 1}}},
    )

    from apps.workspaces.tasks import expire_inactive_schemas, teardown_schema

    # Step 1: the periodic janitor flips the schema to TEARDOWN and dispatches
    # teardown_schema (dispatch is mocked; we invoke the task directly below).
    with patch("apps.workspaces.tasks.teardown_schema.defer_async", new_callable=AsyncMock):
        await expire_inactive_schemas()

    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN

    # Step 2: the DROP fails transiently. The schema reverts to ACTIVE.
    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.side_effect = RuntimeError("lock conflict")
        with pytest.raises(RuntimeError):
            await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    await completed_run.arefresh_from_db()

    # Data is intact and still visible to the catalog.
    assert active_schema.state == SchemaState.ACTIVE
    assert completed_run.state == MaterializationRun.RunState.COMPLETED


# ---------------------------------------------------------------------------
# teardown_schema: dependent view-schema consistency
# ---------------------------------------------------------------------------
#
# A tenant data schema (t_<id>) is SHARED across workspaces. DROP SCHEMA CASCADE
# cascade-drops the namespaced views inside every dependent multi-tenant
# workspace's view schema, so teardown_schema must flip those ACTIVE
# WorkspaceViewSchema rows to FAILED (ACTIVE would be a lie).


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_fails_dependent_multitenant_view_schemas(
    active_schema, tenant, user
):
    """After the physical DROP, the ACTIVE view schema of a multi-tenant
    workspace B (sharing the torn-down tenant) is flipped to FAILED, while a
    single-tenant workspace's view schema is left untouched."""
    # Multi-tenant workspace B: shares `tenant` + a second tenant, ACTIVE view schema.
    extra_tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="teardown-extra", canonical_name="Teardown Extra"
    )
    ws_b = await Workspace.objects.acreate(name="Teardown Sibling B", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=tenant)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=extra_tenant)
    vs_b = await WorkspaceViewSchema.objects.acreate(
        workspace=ws_b, schema_name="ws_teardown_b", state=SchemaState.ACTIVE
    )

    # Single-tenant workspace C: contains only `tenant`. Even if it has a view
    # schema row, it is not multi-tenant, so it must be left untouched.
    ws_c = await Workspace.objects.acreate(name="Teardown Single C", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws_c, tenant=tenant)
    vs_c = await WorkspaceViewSchema.objects.acreate(
        workspace=ws_c, schema_name="ws_teardown_c", state=SchemaState.ACTIVE
    )

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.return_value = None
        await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    await vs_b.arefresh_from_db()
    await vs_c.arefresh_from_db()

    assert active_schema.state == SchemaState.EXPIRED
    # B's namespaced views were cascade-dropped; ACTIVE was a lie → now FAILED.
    assert vs_b.state == SchemaState.FAILED
    # C is single-tenant — its view schema must not be clobbered.
    assert vs_c.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_does_not_clobber_non_active_view_schema(active_schema, tenant, user):
    """A dependent view schema that is already TEARDOWN must keep its lifecycle
    state — only ACTIVE rows are flipped to FAILED."""
    extra_tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="teardown-extra-2", canonical_name="Teardown Extra 2"
    )
    ws_b = await Workspace.objects.acreate(name="Teardown Sibling B2", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=tenant)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=extra_tenant)
    vs_b = await WorkspaceViewSchema.objects.acreate(
        workspace=ws_b, schema_name="ws_teardown_b2", state=SchemaState.TEARDOWN
    )

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown.return_value = None
        await teardown_schema(schema_id=str(active_schema.id))

    await vs_b.arefresh_from_db()
    assert vs_b.state == SchemaState.TEARDOWN


# ---------------------------------------------------------------------------
# teardown_schema on the REFRESH path (arch #236, finding 00#9)
# ---------------------------------------------------------------------------
#
# When a refresh tears down the OLD schema, the tenant still has a NEW ACTIVE
# schema — the data is NOT gone. teardown_schema must therefore NOT mark the
# dependent multi-tenant view schemas permanently FAILED; instead it must defer a
# rebuild so their views are recreated against the surviving schema.


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_rebuilds_dependent_views_when_surviving_active_schema(tenant, user):
    """Refresh path: the torn-down schema has a sibling ACTIVE schema for the same
    tenant. Dependent multi-tenant view schemas must be rebuilt (a rebuild is
    deferred), NOT flipped to FAILED — their data is intact."""
    # Old schema being torn down (refresh already flipped it to TEARDOWN).
    old_schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="t_refresh_old", state=SchemaState.TEARDOWN
    )
    # New schema the refresh just swapped in and activated for the same tenant.
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="t_refresh_new_r1234", state=SchemaState.ACTIVE
    )

    # Dependent multi-tenant workspace B with an ACTIVE view schema.
    extra_tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="teardown-refresh-extra", canonical_name="Refresh Extra"
    )
    ws_b = await Workspace.objects.acreate(name="Teardown Refresh B", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=tenant)
    await WorkspaceTenant.objects.acreate(workspace=ws_b, tenant=extra_tenant)
    vs_b = await WorkspaceViewSchema.objects.acreate(
        workspace=ws_b, schema_name="ws_teardown_refresh_b", state=SchemaState.ACTIVE
    )

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as mock_rebuild,
    ):
        MockManager.return_value.teardown.return_value = None
        await teardown_schema(schema_id=str(old_schema.id))

    await vs_b.arefresh_from_db()
    # Data is intact (new schema ACTIVE) → views are rebuildable, NOT failed.
    assert vs_b.state == SchemaState.ACTIVE
    mock_rebuild.assert_awaited_once_with(workspace_id=str(ws_b.id))
