"""Table listings read the latest *timestamped* synced run.

Postgres sorts NULLs first under ``ORDER BY completed_at DESC``, so a synced run
without ``completed_at`` used to be picked as the latest and its row counts and
null timestamp reported in place of the real last sync.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from apps.workspaces.api.views import _sync_pipeline_list_tables
from apps.workspaces.models import MaterializationRun, SchemaState, TenantSchema
from mcp_server.pipeline_registry import PipelineConfig, SourceConfig
from mcp_server.services.metadata import pipeline_list_tables

SYNCED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

PIPELINE = PipelineConfig(
    name="commcare_sync",
    description="Test pipeline",
    version="1.0",
    provider="commcare",
    sources=[SourceConfig(name="cases", description="Cases")],
)

EXPECTED = [
    {
        "name": "raw_cases",
        "type": "table",
        "description": "Cases",
        "materialized_row_count": 10,
        "row_count_verified": False,
        "materialized_at": SYNCED_AT.isoformat(),
    }
]


@pytest.fixture
def schema_with_timestampless_run(tenant):
    schema = TenantSchema.objects.create(
        tenant=tenant, schema_name="served", state=SchemaState.ACTIVE
    )
    MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=SYNCED_AT,
        result={"sources": {"cases": {"state": "completed", "rows": 10}}},
    )
    MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.PARTIAL,
        result={"sources": {"cases": {"state": "completed", "rows": 999}}},
    )
    return schema


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_mcp_list_tables_ignores_a_run_without_completed_at(schema_with_timestampless_run):
    with patch(
        "mcp_server.services.metadata._live_tables_in_schema",
        AsyncMock(return_value={"raw_cases"}),
    ):
        tables = await pipeline_list_tables(schema_with_timestampless_run, PIPELINE)

    assert tables == EXPECTED


@pytest.mark.django_db
def test_api_list_tables_ignores_a_run_without_completed_at(schema_with_timestampless_run):
    tables = _sync_pipeline_list_tables(schema_with_timestampless_run, PIPELINE, {"raw_cases"})

    assert tables == EXPECTED
