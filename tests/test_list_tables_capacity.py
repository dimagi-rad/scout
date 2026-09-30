"""A full DB connection limit must surface as "busy", never as an empty catalog (#757)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psycopg
import pytest
from django.test import override_settings

from apps.common.capacity import CapacityExhausted, CapacityResource
from apps.common.error_codes import ErrorCode
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
from mcp_server.server import get_metadata, list_tables
from mcp_server.services import metadata
from mcp_server.services.metadata import _live_tables_in_schema, pipeline_list_tables

REFUSAL = psycopg.OperationalError("FATAL:  sorry, too many clients already")
MANAGED_URL = "postgresql://u:p@db.example.com:5432/managed"

PIPELINE = PipelineConfig(
    name="commcare_sync",
    description="Test pipeline",
    version="1.0",
    provider="commcare",
    sources=[SourceConfig(name="cases", description="Cases")],
)


@pytest.fixture
def served_schema(tenant):
    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name="served", state=SchemaState.ACTIVE
    )
    MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC),
        result={"sources": {"cases": {"state": "completed", "rows": 10}}},
    )
    return schema


@pytest.fixture
def refused_connection():
    with patch.object(metadata, "_execute_async_parameterized", AsyncMock(side_effect=REFUSAL)):
        yield


@pytest.fixture
def reported():
    with patch("mcp_server.server.report_capacity_exhausted") as report:
        yield report


def _tool_patches():
    return (
        patch(
            "mcp_server.server._resolve_mcp_context",
            AsyncMock(return_value=SimpleNamespace(schema_name="served", tenant_id="t")),
        ),
        patch("mcp_server.server._resolve_pipeline_config", AsyncMock(return_value=PIPELINE)),
    )


@pytest.mark.asyncio
@override_settings(MANAGED_DATABASE_URL=MANAGED_URL)
async def test_the_live_table_probe_raises_capacity_instead_of_returning_an_empty_set(
    refused_connection,
):
    with pytest.raises(CapacityExhausted) as raised:
        await _live_tables_in_schema("served")

    assert raised.value.resource == CapacityResource.DATABASE


@pytest.mark.asyncio
@override_settings(MANAGED_DATABASE_URL=MANAGED_URL)
async def test_the_live_table_probe_keeps_treating_other_failures_as_nothing_live():
    with patch.object(
        metadata,
        "_execute_async_parameterized",
        AsyncMock(side_effect=psycopg.OperationalError("connection refused")),
    ):
        assert await _live_tables_in_schema("served") == set()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(MANAGED_DATABASE_URL=MANAGED_URL)
async def test_the_catalog_is_never_empty_when_the_database_refused_the_connection(
    served_schema, refused_connection
):
    with pytest.raises(CapacityExhausted):
        await pipeline_list_tables(served_schema, PIPELINE)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(MANAGED_DATABASE_URL=MANAGED_URL)
async def test_list_tables_answers_busy_when_the_connection_limit_is_hit(
    served_schema, refused_connection, reported
):
    context, pipeline = _tool_patches()
    with context, pipeline:
        result = await list_tables()

    assert result["success"] is False
    assert result["error"]["code"] == ErrorCode.CAPACITY_EXHAUSTED
    assert "data" not in result
    reported.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_list_tables_does_not_mask_other_failures_as_busy(served_schema, reported):
    context, pipeline = _tool_patches()
    with (
        context,
        pipeline,
        patch("mcp_server.server.pipeline_list_tables", AsyncMock(side_effect=RuntimeError("bug"))),
        pytest.raises(RuntimeError),
    ):
        await list_tables()

    reported.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@override_settings(MANAGED_DATABASE_URL=MANAGED_URL)
async def test_get_metadata_answers_busy_instead_of_reporting_zero_tables(
    served_schema, refused_connection, reported
):
    context, pipeline = _tool_patches()
    with context, pipeline:
        result = await get_metadata()

    assert result["success"] is False
    assert result["error"]["code"] == ErrorCode.CAPACITY_EXHAUSTED
    reported.assert_called_once()
