"""MCP ``get_schema_status.last_materialized_at`` follows the API's ``last_synced_at`` rule.

A synced (COMPLETED/PARTIAL) run without ``completed_at`` sorts first under
``ORDER BY completed_at DESC`` in Postgres, so it used to shadow every real sync
and report null while the workspace API reported a time (#251 discrepancy 2).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from mcp_server.server import get_schema_status

SYNCED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("multi", [False, True])
async def test_a_run_without_completed_at_does_not_hide_the_last_sync(workspace, tenant, multi):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="served", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=SYNCED_AT,
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=MaterializationRun.RunState.PARTIAL
    )
    if multi:
        other = await Tenant.objects.acreate(provider="commcare", external_id="other-sync")
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
        await WorkspaceViewSchema.objects.acreate(
            workspace=workspace, schema_name="ws_view", state=SchemaState.ACTIVE
        )

    with (
        patch("mcp_server.server.pipeline_list_tables", AsyncMock(return_value=[])),
        patch("mcp_server.server.workspace_list_tables", AsyncMock(return_value=[])),
    ):
        response = await get_schema_status(workspace_id=str(workspace.id))

    assert response["success"] is True
    assert response["data"]["last_materialized_at"] == SYNCED_AT.isoformat()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_single_source_last_sync_counts_every_schema_of_the_source(workspace, tenant):
    """Same scope as the workspace API: any of the source's schemas, not only the serving one."""
    older = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="retired", state=SchemaState.EXPIRED
    )
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="served", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=older,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=SYNCED_AT,
    )

    with patch("mcp_server.server.pipeline_list_tables", AsyncMock(return_value=[])):
        response = await get_schema_status(workspace_id=str(workspace.id))

    assert response["data"]["last_materialized_at"] == SYNCED_AT.isoformat()
