"""Tests for the canonical workspace world-state read-model (arch #251).

The status / last_synced_at assertions are a golden master pinning
``derive_world_state`` to the behavior the status API had before this refactor —
EXCEPT the one intentional change (Decision 3): a PARTIAL run now counts toward
last_synced_at, which gets its own explicit test below.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.world_state import derive_world_state

RunState = MaterializationRun.RunState


async def _make_workspace(tenant_count=1):
    user = await get_user_model().objects.acreate_user(
        email=f"u-{uuid.uuid4().hex}@example.com", password="x"
    )
    workspace = await Workspace.objects.acreate(name="W", created_by=user)
    await WorkspaceMembership.objects.acreate(
        workspace=workspace, user=user, role=WorkspaceRole.MANAGE
    )
    tenants = []
    for _ in range(tenant_count):
        tenant = await Tenant.objects.acreate(
            provider="commcare",
            external_id=f"d-{uuid.uuid4().hex}",
            canonical_name="T",
        )
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=tenant)
        tenants.append(tenant)
    return workspace, tenants


async def _add_schema(tenant, state):
    return await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"s_{uuid.uuid4().hex}", state=state
    )


async def _add_run(schema, state, completed_at):
    return await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare", state=state, completed_at=completed_at
    )


async def _add_view_schema(workspace, state, last_error=""):
    return await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name=f"v_{uuid.uuid4().hex}",
        state=state,
        last_error=last_error,
    )


# ── status golden master: single-tenant ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "schema_state,expected",
    [
        (None, "unavailable"),
        (SchemaState.ACTIVE, "available"),
        (SchemaState.PROVISIONING, "provisioning"),
        (SchemaState.MATERIALIZING, "provisioning"),
        (SchemaState.FAILED, "unavailable"),
        (SchemaState.EXPIRED, "unavailable"),
        (SchemaState.TEARDOWN, "unavailable"),
    ],
)
async def test_single_tenant_status_golden_master(schema_state, expected):
    workspace, (tenant,) = await _make_workspace(tenant_count=1)
    if schema_state is not None:
        await _add_schema(tenant, schema_state)
    ws = await derive_world_state(workspace)
    assert ws.status == expected
    assert ws.is_multi_tenant is False


# ── status golden master: multi-tenant (keyed on view schema) ─────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "view_schema_state,expected",
    [
        (SchemaState.ACTIVE, "available"),
        (SchemaState.FAILED, "failed"),
        (SchemaState.PROVISIONING, "provisioning"),
        (SchemaState.TEARDOWN, "provisioning"),
        (SchemaState.MATERIALIZING, "provisioning"),
        (SchemaState.EXPIRED, "provisioning"),
        (None, "provisioning"),
    ],
)
async def test_multi_tenant_status_golden_master(view_schema_state, expected):
    workspace, tenants = await _make_workspace(tenant_count=2)
    for t in tenants:
        await _add_schema(t, SchemaState.ACTIVE)
    if view_schema_state is not None:
        await _add_view_schema(workspace, view_schema_state)
    ws = await derive_world_state(workspace)
    assert ws.status == expected
    assert ws.is_multi_tenant is True


# ── last_synced_at golden master (COMPLETED behavior unchanged) ───────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_last_synced_none_without_runs():
    workspace, (tenant,) = await _make_workspace()
    await _add_schema(tenant, SchemaState.ACTIVE)
    ws = await derive_world_state(workspace)
    assert ws.last_synced_at is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_last_synced_returns_latest_completed():
    workspace, (tenant,) = await _make_workspace()
    schema = await _add_schema(tenant, SchemaState.ACTIVE)
    now = timezone.now()
    await _add_run(schema, RunState.COMPLETED, now - timedelta(hours=2))
    latest = await _add_run(schema, RunState.COMPLETED, now)
    ws = await derive_world_state(workspace)
    assert ws.last_synced_at == latest.completed_at


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_last_synced_ignores_in_flight_and_failed():
    workspace, (tenant,) = await _make_workspace()
    schema = await _add_schema(tenant, SchemaState.ACTIVE)
    now = timezone.now()
    completed = await _add_run(schema, RunState.COMPLETED, now - timedelta(hours=1))
    await _add_run(schema, RunState.LOADING, None)
    await _add_run(schema, RunState.FAILED, now)
    ws = await derive_world_state(workspace)
    assert ws.last_synced_at == completed.completed_at


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_last_synced_max_across_tenants():
    workspace, tenants = await _make_workspace(tenant_count=2)
    schemas = [await _add_schema(t, SchemaState.ACTIVE) for t in tenants]
    now = timezone.now()
    await _add_run(schemas[0], RunState.COMPLETED, now - timedelta(hours=3))
    latest = await _add_run(schemas[1], RunState.COMPLETED, now)
    ws = await derive_world_state(workspace)
    assert ws.last_synced_at == latest.completed_at


# ── Decision 3: PARTIAL now counts toward last_synced_at (intentional change) ─


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_partial_run_now_sets_last_synced():
    """A PARTIAL run did load data, so it sets last_synced_at. The old COMPLETED-
    only derivation returned None for a PARTIAL-only workspace — this is the one
    intended behavior change in Phase 1."""
    workspace, (tenant,) = await _make_workspace()
    schema = await _add_schema(tenant, SchemaState.ACTIVE)
    now = timezone.now()
    partial = await _add_run(schema, RunState.PARTIAL, now)
    ws = await derive_world_state(workspace)
    assert ws.last_synced_at is not None
    assert ws.last_synced_at == partial.completed_at


# ── in_progress ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("active_state", sorted(MaterializationRun.ACTIVE_STATES))
async def test_in_progress_true_for_active_run(active_state):
    workspace, (tenant,) = await _make_workspace()
    schema = await _add_schema(tenant, SchemaState.PROVISIONING)
    await _add_run(schema, active_state, None)
    ws = await derive_world_state(workspace)
    assert ws.in_progress is True


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_in_progress_false_when_only_terminal_runs():
    workspace, (tenant,) = await _make_workspace()
    schema = await _add_schema(tenant, SchemaState.ACTIVE)
    await _add_run(schema, RunState.COMPLETED, timezone.now())
    ws = await derive_world_state(workspace)
    assert ws.in_progress is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("vs_state", [SchemaState.PROVISIONING, SchemaState.TEARDOWN])
async def test_in_progress_true_during_view_schema_rebuild(vs_state):
    workspace, tenants = await _make_workspace(tenant_count=2)
    for t in tenants:
        await _add_schema(t, SchemaState.ACTIVE)
    await _add_view_schema(workspace, vs_state)
    ws = await derive_world_state(workspace)
    assert ws.in_progress is True


# ── last_error ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_last_error_surfaced_from_view_schema():
    workspace, tenants = await _make_workspace(tenant_count=2)
    for t in tenants:
        await _add_schema(t, SchemaState.ACTIVE)
    await _add_view_schema(workspace, SchemaState.FAILED, last_error="build blew up")
    ws = await derive_world_state(workspace)
    assert ws.status == "failed"
    assert ws.last_error == "build blew up"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_last_error_none_for_single_tenant():
    workspace, (tenant,) = await _make_workspace()
    await _add_schema(tenant, SchemaState.ACTIVE)
    ws = await derive_world_state(workspace)
    assert ws.last_error is None
