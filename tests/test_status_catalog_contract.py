"""Cross-surface agreement for the canonical world-state read-model (arch #251).

Three surfaces answer "is this workspace's data loaded, and is anything in
flight": the workspace status API, the agent prompt's ``## Data Availability``
block, and the MCP ``get_schema_status`` tool. Before arch #251 each derived the
answer independently and drifted (a materialization the prompt couldn't see, a
``last_synced_at`` that ignored PARTIAL runs, a ``tables: []`` that lied after a
successful run). These tests pin all three to a single ``derive_world_state`` so
that divergence can't silently accumulate again.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.graph.base import (
    _fetch_multi_tenant_schema_context,
    _fetch_schema_context,
)
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.catalog import CatalogTable
from apps.workspaces.services.world_state import derive_world_state
from mcp_server.server import get_schema_status

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
    return workspace, user, tenants


async def _add_schema(tenant, state):
    from apps.workspaces.models import TenantSchema

    return await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"s_{uuid.uuid4().hex[:12]}", state=state
    )


async def _add_run(schema, state, completed_at):
    return await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare", state=state, completed_at=completed_at
    )


async def _add_view_schema(workspace, state):
    return await WorkspaceViewSchema.objects.acreate(
        workspace=workspace, schema_name=f"ws_{uuid.uuid4().hex[:12]}", state=state
    )


# ── (a) multi-tenant rebuild window: all three surfaces agree "in progress" ──


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cross_surface_agreement_multi_tenant_rebuild_window():
    """A multi-tenant workspace whose view schema is mid-rebuild (TEARDOWN) with a
    prior COMPLETED run: status API, prompt, and get_schema_status must all report
    the same in-progress / provisioning world-state and the same last-synced time."""
    workspace, user, tenants = await _make_workspace(tenant_count=2)
    schemas = [await _add_schema(t, SchemaState.ACTIVE) for t in tenants]
    synced_at = timezone.now()
    await _add_run(schemas[0], RunState.COMPLETED, synced_at)
    await _add_view_schema(workspace, SchemaState.TEARDOWN)

    # Status API surface (WorkspaceDetailView serializes exactly this).
    world = await derive_world_state(workspace)
    assert world.status == "provisioning"
    assert world.in_progress is True
    assert world.last_synced_at == synced_at

    # Prompt surface.
    prompt = await _fetch_multi_tenant_schema_context(workspace, user)
    assert "in progress" in prompt.lower()
    assert "run_materialization" not in prompt

    # MCP tool surface.
    result = await get_schema_status(workspace_id=str(workspace.id))
    assert result["success"] is True
    assert result["data"]["exists"] is True  # not "not_provisioned" during a rebuild
    assert result["data"]["state"] == world.status
    assert result["data"]["last_materialized_at"] == synced_at.isoformat()


# ── (b) PARTIAL-run case: all three agree "available" + last-synced set ──────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cross_surface_agreement_partial_run():
    """A single-tenant workspace whose only run is PARTIAL: it loaded data, so all
    three surfaces report available + a last-synced timestamp (divergence 1)."""
    workspace, user, (tenant,) = await _make_workspace(tenant_count=1)
    schema = await _add_schema(tenant, SchemaState.ACTIVE)
    synced_at = timezone.now()
    await _add_run(schema, RunState.PARTIAL, synced_at)

    world = await derive_world_state(workspace)
    assert world.status == "available"
    assert world.in_progress is False
    assert world.last_synced_at == synced_at

    listed = [
        CatalogTable(
            name="raw_cases",
            type="source",
            logical_name="raw_cases",
            description="",
            row_count=42,
            materialized_at=synced_at.isoformat(),
            verified=True,
        )
    ]

    # Prompt surface (the canonical catalog is mocked; world-state derivation is real).
    with (
        patch(
            "apps.agents.graph.base.list_catalog",
            new=AsyncMock(return_value=listed),
        ),
        patch(
            "apps.agents.graph.base.load_tenant_context",
            new=AsyncMock(side_effect=RuntimeError("no managed DB in tests")),
        ),
        patch(
            "apps.transformations.services.lineage.aget_terminal_assets",
            new=AsyncMock(return_value=[]),
        ),
    ):
        prompt = await _fetch_schema_context(workspace, tenant, user)
    assert "Data is loaded and ready" in prompt
    assert synced_at.isoformat() in prompt

    # MCP tool surface (canonical catalog mocked; must NOT read the dead result key).
    with patch(
        "mcp_server.server.list_catalog",
        new=AsyncMock(return_value=listed),
    ):
        result = await get_schema_status(workspace_id=str(workspace.id))
    assert result["success"] is True
    assert result["data"]["state"] == world.status == "available"
    assert result["data"]["last_materialized_at"] == synced_at.isoformat()
    assert result["data"]["tables"] != []
    assert result["data"]["tables"][0]["name"] == "raw_cases"


# ── single-tenant "do NOT trigger another" now fires on an ACTIVE_STATES run ──


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("active_state", sorted(MaterializationRun.ACTIVE_STATES))
async def test_single_tenant_in_progress_branch_now_fires(active_state):
    """Under the old ``SchemaState.MATERIALIZING`` gate this branch could never
    fire; it must now fire whenever a run is in ACTIVE_STATES (arch #251, div 4)."""
    workspace, user, (tenant,) = await _make_workspace(tenant_count=1)
    schema = await _add_schema(tenant, SchemaState.PROVISIONING)
    await _add_run(schema, active_state, None)

    prompt = await _fetch_schema_context(workspace, tenant, user, interactive=True)

    assert "do not" in prompt.lower()
    assert "trigger another" in prompt.lower()
    assert "No data has been loaded yet" not in prompt


# ── get_schema_status single-tenant no longer lies with tables: [] ───────────


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_get_schema_status_single_tenant_lists_tables_after_success():
    """After a COMPLETED run, get_schema_status must surface tables via the
    reconciled lister — the old ``last_run.result["tables"]`` dead-key read always
    returned [] because the materializer never persists a ``tables`` key."""
    workspace, _user, (tenant,) = await _make_workspace(tenant_count=1)
    schema = await _add_schema(tenant, SchemaState.ACTIVE)
    await _add_run(schema, RunState.COMPLETED, timezone.now())

    listed = [
        CatalogTable(
            name="raw_cases",
            type="source",
            logical_name="raw_cases",
            description="",
            row_count=10,
            materialized_at=None,
            verified=True,
        )
    ]
    with patch(
        "mcp_server.server.list_catalog",
        new=AsyncMock(return_value=listed),
    ):
        result = await get_schema_status(workspace_id=str(workspace.id))

    assert result["success"] is True
    assert result["data"]["exists"] is True
    assert result["data"]["tables"] != []
    assert result["data"]["tables"][0]["name"] == "raw_cases"
