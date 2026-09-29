"""Pipeline resolution reads the latest *timestamped* synced run.

Postgres sorts NULLs first under ``ORDER BY completed_at DESC``, so a synced run
without ``completed_at`` used to be picked as the latest and its pipeline used
to describe tables another pipeline wrote.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from apps.semantic.services import catalog as catalog_service
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from mcp_server import server as mcp_server_module
from mcp_server.context import QueryContext

# Neither is the commcare tenant's provider fallback, so the assertion proves
# which run was read rather than that no run was.
SYNCED_PIPELINE = "ocs_sync"
TIMESTAMPLESS_PIPELINE = "connect_sync"


@pytest.fixture
def schema_with_timestampless_run(tenant):
    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name="served", state=SchemaState.ACTIVE
    )
    MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline=SYNCED_PIPELINE,
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC),
    )
    MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline=TIMESTAMPLESS_PIPELINE,
        state=MaterializationRun.RunState.PARTIAL,
    )
    return schema


def _context(schema):
    return AsyncMock(
        return_value=QueryContext(
            tenant_id=str(schema.tenant_id), schema_name=schema.schema_name, connection_params={}
        )
    )


@pytest.fixture
def mcp_spies(monkeypatch, schema_with_timestampless_run):
    monkeypatch.setattr(
        mcp_server_module, "load_workspace_context", _context(schema_with_timestampless_run)
    )
    resolved = []

    async def spy_list_tables(ts, pipeline_config):
        resolved.append(pipeline_config.name)
        return []

    async def spy_describe(table_name, ctx, tenant_metadata, pipeline_config):
        resolved.append(pipeline_config.name)
        return {"name": table_name, "description": "", "columns": []}

    async def spy_metadata(ts, ctx, tenant_metadata, pipeline_config):
        resolved.append(pipeline_config.name)
        return {"tables": {}, "relationships": []}

    monkeypatch.setattr(mcp_server_module, "pipeline_list_tables", spy_list_tables)
    monkeypatch.setattr(mcp_server_module, "pipeline_describe_table", spy_describe)
    monkeypatch.setattr(mcp_server_module, "pipeline_get_metadata", spy_metadata)
    return resolved


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "call",
    [
        lambda ws: mcp_server_module.list_tables(workspace_id=ws),
        lambda ws: mcp_server_module.describe_table("raw_cases", workspace_id=ws),
        lambda ws: mcp_server_module.get_metadata(workspace_id=ws),
    ],
    ids=["list_tables", "describe_table", "get_metadata"],
)
async def test_mcp_metadata_tools_ignore_a_run_without_completed_at(mcp_spies, call):
    result = await call(str(uuid.uuid4()))

    assert result["success"] is True
    assert mcp_spies == [SYNCED_PIPELINE]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_schema_status_ignores_a_run_without_completed_at(mcp_spies, workspace):
    result = await mcp_server_module.get_schema_status(workspace_id=str(workspace.id))

    assert result["success"] is True
    assert mcp_spies == [SYNCED_PIPELINE]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_semantic_catalog_ignores_a_run_without_completed_at(
    monkeypatch, workspace, schema_with_timestampless_run
):
    monkeypatch.setattr(
        catalog_service, "load_workspace_context", _context(schema_with_timestampless_run)
    )
    monkeypatch.setattr(catalog_service, "pipeline_table_primary_keys", AsyncMock(return_value={}))
    resolved = []

    async def spy_list_tables(ts, pipeline_config):
        resolved.append(pipeline_config.name)
        return []

    monkeypatch.setattr(catalog_service, "pipeline_list_tables", spy_list_tables)

    await catalog_service._load_physical_tables_async(workspace)

    assert resolved == [SYNCED_PIPELINE]
