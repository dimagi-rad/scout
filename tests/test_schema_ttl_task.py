"""Tests for schema TTL tasks."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg.errors
import pytest
from asgiref.sync import sync_to_async
from django.db import InterfaceError as DjangoInterfaceError
from django.db import ProgrammingError as DjangoProgrammingError
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
from apps.workspaces.services.data_operation import DataLockTimeout
from apps.workspaces.services.schema_manager import SchemaManager, SchemaStillReferenced
from apps.workspaces.tasks import (
    _RETIRE_MAX_ATTEMPTS,
    _RETIRE_RETRY_BASE_SECONDS,
    expire_inactive_schemas,
    teardown_schema,
)
from tests.tenant_lock_probe import try_tenant_data_lock


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
    # Production flips the row to TEARDOWN before dispatching teardown_schema;
    # the state CAS (arch #237) only drops a row still in TEARDOWN.
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.retire_tenant_schema.return_value = None
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
    # Production flips the row to TEARDOWN before dispatch; the CAS (arch #237)
    # only drops a row still in TEARDOWN.
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
    failed_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.FAILED,
    )

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.retire_tenant_schema.return_value = None
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
        MockManager.return_value.retire_tenant_schema.side_effect = RuntimeError("DB error")
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
        MockManager.return_value.retire_tenant_schema.side_effect = RuntimeError("DB error")
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
        MockManager.return_value.retire_tenant_schema.side_effect = RuntimeError("lock conflict")
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
    # Production flips the row to TEARDOWN before dispatch; the CAS (arch #237)
    # only drops a row still in TEARDOWN.
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

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
        MockManager.return_value.retire_tenant_schema.return_value = None
        await teardown_schema(schema_id=str(active_schema.id))

    await active_schema.arefresh_from_db()
    await vs_b.arefresh_from_db()
    await vs_c.arefresh_from_db()

    assert active_schema.state == SchemaState.EXPIRED
    # B's namespaced views were cascade-dropped; ACTIVE was a lie → now FAILED.
    assert vs_b.state == SchemaState.FAILED
    # 07#9: the FAILED row must carry a TRUTHFUL last_error explaining the
    # teardown cascade (marked) — not the empty fallback that get_schema_status
    # would render as the generic "View schema build failed." and that the resume
    # prompt would misread as "a system-side fix is required, do NOT re-run".
    from apps.workspaces.models import VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER

    assert VIEW_SCHEMA_CASCADE_TEARDOWN_MARKER in vs_b.last_error
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
        MockManager.return_value.retire_tenant_schema.return_value = None
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
        MockManager.return_value.retire_tenant_schema.return_value = None
        await teardown_schema(schema_id=str(old_schema.id))

    await vs_b.arefresh_from_db()
    # Data is intact (new schema ACTIVE) → views are rebuildable, NOT failed.
    assert vs_b.state == SchemaState.ACTIVE
    mock_rebuild.assert_awaited_once_with(workspace_id=str(ws_b.id))


# ---------------------------------------------------------------------------
# teardown state CAS (arch #237, finding 03#0)
# ---------------------------------------------------------------------------
#
# provision() resurrects TEARDOWN/EXPIRED rows back to ACTIVE (the 2026-06-10
# incident-b fix). A teardown task that was already queued before the resurrection
# fetches the row by id and would otherwise DROP SCHEMA CASCADE unconditionally —
# destroying the freshly re-provisioned data. The task must re-read the row's state
# and ABORT the drop when the row is no longer TEARDOWN.


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_aborts_when_row_resurrected_to_active(active_schema):
    """A stale queued teardown_schema must NOT drop a schema that provision()
    resurrected to ACTIVE between enqueue and execution. The CAS holds: the
    physical DROP is skipped and the schema stays ACTIVE."""
    # The row is ACTIVE (active_schema fixture) — i.e. it was resurrected after the
    # teardown was queued (which would have required it to be TEARDOWN at enqueue).
    assert active_schema.state == SchemaState.ACTIVE

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        await teardown_schema(schema_id=str(active_schema.id))

    # The drop never happened — the manager was never asked to teardown.
    MockManager.return_value.retire_tenant_schema.assert_not_called()

    await active_schema.arefresh_from_db()
    # The resurrected schema is untouched.
    assert active_schema.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_aborts_does_not_stale_runs(active_schema):
    """When the CAS aborts the drop, data-bearing runs must remain terminal —
    they must NOT be flipped to STALE, since the schema/tables are still present."""
    completed_run = await MaterializationRun.objects.acreate(
        tenant_schema=active_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        result={"sources": {"cases": {"state": "completed", "rows": 1}}},
    )

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        await teardown_schema(schema_id=str(active_schema.id))

    MockManager.return_value.retire_tenant_schema.assert_not_called()

    await completed_run.arefresh_from_db()
    assert completed_run.state == MaterializationRun.RunState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_schema_still_drops_when_state_is_teardown(active_schema):
    """Sanity: the CAS does not break the normal path — a row legitimately in
    TEARDOWN is still dropped and marked EXPIRED."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.retire_tenant_schema.return_value = None
        await teardown_schema(schema_id=str(active_schema.id))

    MockManager.return_value.retire_tenant_schema.assert_called_once()
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.EXPIRED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_view_schema_aborts_when_row_resurrected_to_active(db, user):
    """A stale queued teardown_view_schema_task must NOT drop a view schema that
    was reactivated (ACTIVE) between enqueue and execution."""
    workspace = await Workspace.objects.acreate(name="CAS view ws", created_by=user)
    vs = await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name="ws_cas_active", state=SchemaState.ACTIVE
    )

    from apps.workspaces.tasks import teardown_view_schema_task

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        await teardown_view_schema_task(view_schema_id=str(vs.id))

    MockManager.return_value.teardown_view_schema.assert_not_called()

    await vs.arefresh_from_db()
    assert vs.state == SchemaState.ACTIVE


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_view_schema_still_drops_when_state_is_teardown(db, user):
    """Sanity: a view schema legitimately in TEARDOWN is still dropped and
    marked EXPIRED."""
    workspace = await Workspace.objects.acreate(name="CAS view ws 2", created_by=user)
    vs = await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name="ws_cas_teardown", state=SchemaState.TEARDOWN
    )

    from apps.workspaces.tasks import teardown_view_schema_task

    with patch("apps.workspaces.tasks.SchemaManager") as MockManager:
        MockManager.return_value.teardown_view_schema.return_value = None
        await teardown_view_schema_task(view_schema_id=str(vs.id))

    MockManager.return_value.teardown_view_schema.assert_called_once()
    await vs.arefresh_from_db()
    assert vs.state == SchemaState.EXPIRED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_teardown_preserves_view_that_excluded_the_source(active_schema, tenant, user):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])
    other = await Tenant.objects.acreate(provider="commcare", external_id="remaining-source")
    workspace = await Workspace.objects.acreate(name="Degraded", created_by=user)
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
    await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
    view = await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="ws_degraded_teardown",
        state=SchemaState.ACTIVE,
        tenant_coverage={
            "included_tenants": [{"tenant_id": str(other.id)}],
            "excluded_tenants": [{"tenant_id": str(tenant.id)}],
        },
    )
    with patch("apps.workspaces.tasks.SchemaManager"):
        await teardown_schema(schema_id=str(active_schema.id))
    await view.arefresh_from_db()
    assert view.state == SchemaState.ACTIVE
    assert view.last_error == ""


# ---------------------------------------------------------------------------
# teardown_schema: retirement retries
# ---------------------------------------------------------------------------


async def _superseded_teardown_schema(active_schema, tenant):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="ttl_test_schema_r", state=SchemaState.ACTIVE
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_failed_retirement_of_a_superseded_schema_is_retried(active_schema, tenant):
    """Nothing else re-arms a TEARDOWN row, so an unclassified failure must
    reschedule rather than strand the physical schema forever."""
    await _superseded_teardown_schema(active_schema, tenant)

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        MockManager.return_value.retire_tenant_schema.side_effect = RuntimeError("dropped")
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id), attempt=2)

    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(active_schema.id), attempt=3
    )
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_retirement_stops_retrying_after_the_attempt_cap(active_schema, tenant):
    await _superseded_teardown_schema(active_schema, tenant)

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        MockManager.return_value.retire_tenant_schema.side_effect = RuntimeError("still broken")
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id), attempt=_RETIRE_MAX_ATTEMPTS - 1)

    retry.return_value.defer_async.assert_not_awaited()
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "failure",
    [
        psycopg.errors.LockNotAvailable("lock timeout"),
        psycopg.errors.DeadlockDetected("deadlock detected"),
    ],
)
async def test_lock_contention_keeps_the_only_schema_in_teardown_and_retries(
    active_schema, failure
):
    """Nothing was dropped and no other schema serves, yet a lock timeout is not a
    reason to resurrect: the row stays TEARDOWN and retirement is rescheduled."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        MockManager.return_value.retire_tenant_schema.side_effect = failure
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_awaited_once()
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_tenant_lock_timeout_reschedules_instead_of_stranding(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    @asynccontextmanager
    async def never_granted(_tenant_ids):
        raise DataLockTimeout("T held by a long load")
        yield

    with (
        patch("apps.workspaces.tasks.tenant_data_lock_if_free", never_granted),
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    MockManager.return_value.retire_tenant_schema.assert_not_called()
    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(active_schema.id), attempt=1
    )
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_busy_tenant_lock_defers_retirement_without_waiting_and_spends_an_attempt(
    active_schema,
):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        try_tenant_data_lock(active_schema.tenant_id) as held,
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        assert held
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await asyncio.wait_for(teardown_schema(schema_id=str(active_schema.id)), timeout=3)

    MockManager.return_value.retire_tenant_schema.assert_not_called()
    retry.assert_called_once_with(schedule_in={"seconds": _RETIRE_RETRY_BASE_SECONDS})
    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(active_schema.id), attempt=1
    )
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_busy_tenant_lock_gives_up_at_the_retirement_attempt_cap(active_schema, caplog):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    @asynccontextmanager
    async def busy_lock(_tenant_id):
        yield False

    with (
        patch("apps.workspaces.tasks.tenant_data_lock_if_free", busy_lock),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
        caplog.at_level(logging.ERROR, logger="apps.workspaces.tasks"),
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id), attempt=_RETIRE_MAX_ATTEMPTS - 1)

    retry.return_value.defer_async.assert_not_awaited()
    assert "giving up retiring schema" in caplog.text
    assert "tenant lock T is held by an active writer" in caplog.text
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_unlisted_leftover_object_gives_up_at_once(active_schema):
    """A type or function left inside the schema is not something a rebuild moves."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        MockManager.return_value.retire_tenant_schema.side_effect = SchemaStillReferenced(
            active_schema.schema_name,
            [],
            detail="type leftover depends on schema",
            converges=False,
        )
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_not_awaited()
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_schema_deleted_while_waiting_for_t_is_a_no_op(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    @asynccontextmanager
    async def delete_while_waiting(_tenant_ids):
        await TenantSchema.objects.filter(pk=active_schema.pk).adelete()
        yield True

    with (
        patch("apps.workspaces.tasks.tenant_data_lock_if_free", delete_while_waiting),
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    MockManager.return_value.retire_tenant_schema.assert_not_called()
    retry.return_value.defer_async.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_statement_timeout_during_retirement_retries_instead_of_resurrecting(
    active_schema,
):
    """A statement_timeout (57014) at or below the retire lock timeout is lock
    contention too; it must not flip the only schema back to ACTIVE."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        MockManager.return_value.retire_tenant_schema.side_effect = psycopg.errors.QueryCanceled(
            "canceling statement due to statement timeout"
        )
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(active_schema.id), attempt=1
    )
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_dependent_that_is_not_a_view_schema_gives_up_at_once(active_schema):
    """A dbt view in another tenant schema is nothing a rebuild can move."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        MockManager.return_value.retire_tenant_schema.side_effect = SchemaStillReferenced(
            active_schema.schema_name,
            [{"schema": "t_other_tenant", "name": "stg_cases", "relkind": "v"}],
        )
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_not_awaited()
    await active_schema.arefresh_from_db()
    assert active_schema.state == SchemaState.TEARDOWN


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_retirement_never_rebuilds_an_expired_dependent_view_schema(
    active_schema, tenant, user
):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])
    workspace = await Workspace.objects.acreate(name="Expired dependent", created_by=user)
    expired = await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name="ws_expired0000000", state=SchemaState.EXPIRED
    )

    with (
        patch("apps.workspaces.tasks.SchemaManager") as MockManager,
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
        patch(
            "apps.workspaces.tasks.rebuild_workspace_view_schema.defer_async",
            new_callable=AsyncMock,
        ) as rebuild,
    ):
        MockManager.return_value.retire_tenant_schema.side_effect = SchemaStillReferenced(
            active_schema.schema_name,
            [{"schema": expired.schema_name, "name": "v", "relkind": "v"}],
        )
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    rebuild.assert_not_awaited()
    # Its views are already gone, so the next attempt can retire: keep retrying.
    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(active_schema.id), attempt=1
    )
    await expired.arefresh_from_db()
    assert expired.state == SchemaState.EXPIRED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_query_bug_taking_t_is_not_retried_as_contention(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    @asynccontextmanager
    async def buggy_session(_tenant_ids):
        raise psycopg.errors.UndefinedFunction("function pg_advisory_lock_x does not exist")
        yield

    with (
        patch("apps.workspaces.tasks.tenant_data_lock_if_free", buggy_session),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
        pytest.raises(psycopg.errors.UndefinedFunction),
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_interface_error_taking_t_reschedules_instead_of_crashing(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    @asynccontextmanager
    async def broken_session(_tenant_ids):
        raise psycopg.InterfaceError("connection already closed")
        yield

    with (
        patch("apps.workspaces.tasks.tenant_data_lock_if_free", broken_session),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(active_schema.id), attempt=1
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_dropped_orm_connection_while_waiting_for_t_reschedules(active_schema):
    """After up to 30 minutes waiting for T, the ORM connection may be dead; Django
    raises its own InterfaceError, which sits outside DatabaseError."""
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        patch.object(
            TenantSchema,
            "arefresh_from_db",
            AsyncMock(side_effect=DjangoInterfaceError("connection already closed")),
        ),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_awaited_once_with(
        schema_id=str(active_schema.id), attempt=1
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_orm_query_bug_while_re_reading_the_row_surfaces(active_schema):
    active_schema.state = SchemaState.TEARDOWN
    await active_schema.asave(update_fields=["state"])

    with (
        patch.object(
            TenantSchema,
            "arefresh_from_db",
            AsyncMock(side_effect=DjangoProgrammingError("column does not exist")),
        ),
        patch("apps.workspaces.tasks.teardown_schema.configure") as retry,
        pytest.raises(DjangoProgrammingError),
    ):
        retry.return_value.defer_async = AsyncMock(return_value=1)
        await teardown_schema(schema_id=str(active_schema.id))

    retry.return_value.defer_async.assert_not_awaited()
