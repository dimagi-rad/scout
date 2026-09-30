"""A full DB connection limit during a catalog rebuild is busy, never stale or degraded (#775)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import psycopg
import pytest
from asgiref.sync import async_to_sync

from apps.common.capacity import BUSY_MESSAGE, CapacityExhausted, CapacityResource
from apps.semantic.models import CubeSchema, CustomDataset, SemanticDataset, SemanticModel
from apps.semantic.services import catalog, cube_schema, custom_datasets
from apps.semantic.services.catalog import (
    PhysicalTable,
    SemanticCatalogUnavailable,
    ensure_semantic_model,
    load_physical_tables,
)
from apps.semantic.services.custom_datasets import CustomDatasetError, infer_custom_dataset_columns
from apps.workspaces.models import SchemaState, WorkspaceViewSchema
from apps.workspaces.services.query_state import semantic_layer_state
from mcp_server.services import metadata
from mcp_server.services.metadata import pipeline_table_primary_keys, workspace_table_identity

REFUSAL = psycopg.OperationalError("FATAL:  sorry, too many clients already")
FULL = CapacityExhausted(CapacityResource.DATABASE, "full")
CTX = SimpleNamespace(
    schema_name="ws_fixture",
    readonly_role="role",
    max_query_timeout_seconds=10,
    connection_params={},
)


@pytest.fixture
def reported():
    with patch.object(cube_schema, "report_capacity_exhausted") as report:
        yield report


@pytest.fixture
def serving_model(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Serving", status="active")
    SemanticDataset.objects.create(
        workspace=workspace,
        semantic_model=model,
        name="visits",
        table_name="raw_visits",
        primary_key="id",
    )
    return model


@pytest.mark.django_db
@pytest.mark.parametrize("error", [FULL, REFUSAL], ids=["already-classified", "raw-refusal"])
def test_a_refused_connection_while_reading_tables_is_not_a_refresh_prompt(workspace, error):
    with (
        patch.object(catalog, "_load_physical_tables_async", AsyncMock(side_effect=error)),
        pytest.raises(CapacityExhausted) as raised,
    ):
        load_physical_tables(workspace)

    assert raised.value.resource == CapacityResource.DATABASE
    assert raised.value.__cause__ is not raised.value


@pytest.mark.django_db
def test_other_read_failures_still_ask_for_a_refresh(workspace):
    with (
        patch.object(catalog, "_load_physical_tables_async", AsyncMock(side_effect=ValueError)),
        pytest.raises(SemanticCatalogUnavailable, match="refresh workspace data"),
    ):
        load_physical_tables(workspace)


@pytest.mark.asyncio
async def test_a_refused_primary_key_probe_is_raised_not_swallowed():
    with (
        patch.object(metadata, "_execute_async_parameterized", AsyncMock(side_effect=REFUSAL)),
        pytest.raises(CapacityExhausted),
    ):
        await pipeline_table_primary_keys(CTX)


@pytest.mark.asyncio
async def test_another_primary_key_probe_failure_still_yields_no_keys():
    with patch.object(metadata, "_execute_async_parameterized", AsyncMock(side_effect=ValueError)):
        assert await pipeline_table_primary_keys(CTX) == {}


@pytest.fixture
def view_schema(workspace):
    return WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name=CTX.schema_name, state=SchemaState.ACTIVE
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_refused_identity_probe_is_raised_not_reported_unverified(workspace, view_schema):
    with (
        patch.object(metadata, "parse_view_sources", return_value={"t": SimpleNamespace()}),
        patch.object(metadata, "workspace_list_tables", AsyncMock(side_effect=REFUSAL)),
        pytest.raises(CapacityExhausted),
    ):
        await workspace_table_identity(workspace.id, CTX, "t", [])


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_another_identity_probe_failure_is_still_unverified(workspace, view_schema):
    with (
        patch.object(metadata, "parse_view_sources", return_value={"t": SimpleNamespace()}),
        patch.object(metadata, "workspace_list_tables", AsyncMock(side_effect=ValueError)),
    ):
        identity = await workspace_table_identity(workspace.id, CTX, "t", [])

    assert identity == metadata.unverified_source_identity()


@pytest.mark.django_db(transaction=True)
def test_a_capacity_refusal_defers_the_rebuild_without_an_error_diagnostic(
    workspace, serving_model, reported
):
    serving_model.metadata = {"last_build": {"ok": True}}
    serving_model.save()
    CubeSchema.objects.create(
        workspace=workspace,
        semantic_model=serving_model,
        status=CubeSchema.Status.ACTIVE,
        content="cubes: []",
        content_hash="a" * 64,
        filename="serving.yaml",
    )

    with (
        patch.object(catalog, "_load_physical_tables_async", AsyncMock(side_effect=REFUSAL)),
        pytest.raises(CapacityExhausted) as raised,
    ):
        cube_schema.build_and_promote_cube_schema(workspace)

    serving_model.refresh_from_db()
    assert str(raised.value) == BUSY_MESSAGE
    assert serving_model.status == SemanticModel.Status.ACTIVE
    last_build = serving_model.metadata["last_build"]
    assert (last_build["ok"], last_build["status"]) == (False, "deferred")
    assert last_build["reason"] == BUSY_MESSAGE
    assert "error" not in last_build
    assert serving_model.diagnostics == []
    assert async_to_sync(semantic_layer_state)(workspace)[0] == "stale"
    reported.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_a_capacity_refusal_defers_an_unserved_model_without_flipping_it_to_error(
    workspace, serving_model, reported
):
    with (
        patch.object(catalog, "_load_physical_tables_async", AsyncMock(side_effect=FULL)),
        pytest.raises(CapacityExhausted),
    ):
        cube_schema.build_and_promote_cube_schema(workspace)

    serving_model.refresh_from_db()
    assert serving_model.status == SemanticModel.Status.ACTIVE
    assert serving_model.metadata["last_build"]["status"] == "deferred"


@pytest.mark.django_db
def test_other_build_failures_are_still_recorded(workspace, serving_model):
    cube_schema._record_build_failure(workspace, serving_model, ValueError("broken"))

    serving_model.refresh_from_db()
    assert serving_model.metadata["last_build"]["ok"] is False


@pytest.mark.django_db
def test_a_refused_custom_dataset_probe_is_raised_not_a_validation_error():
    with (
        patch.object(custom_datasets, "load_workspace_context", AsyncMock(return_value=CTX)),
        patch.object(custom_datasets.psycopg, "connect", side_effect=REFUSAL),
        pytest.raises(CapacityExhausted),
    ):
        infer_custom_dataset_columns(SimpleNamespace(id="w"), "SELECT 1")


@pytest.mark.django_db
def test_other_custom_dataset_probe_failures_are_validation_errors():
    with (
        patch.object(custom_datasets, "load_workspace_context", AsyncMock(return_value=CTX)),
        patch.object(custom_datasets.psycopg, "connect", side_effect=ValueError("bad")),
        pytest.raises(CustomDatasetError, match="failed validation"),
    ):
        infer_custom_dataset_columns(SimpleNamespace(id="w"), "SELECT 1")


@pytest.mark.django_db(transaction=True)
def test_a_refused_custom_probe_keeps_the_dataset_active_and_rolls_the_rebuild_back(workspace):
    custom = CustomDataset.objects.create(
        workspace=workspace, name="visit_stats", definition_sql="SELECT * FROM visits"
    )
    table = PhysicalTable(
        name="visits", type="table", description="", columns=[{"name": "id", "type": "integer"}]
    )
    with (
        patch.object(catalog, "load_physical_tables", return_value=("fixture", [table])),
        patch.object(catalog, "compile_custom_dataset_sql", return_value="SELECT 1"),
        patch.object(catalog, "infer_custom_dataset_columns", side_effect=FULL),
        pytest.raises(CapacityExhausted),
    ):
        ensure_semantic_model(workspace)

    custom.refresh_from_db()
    assert custom.status == CustomDataset.Status.ACTIVE
    assert custom.diagnostics == []
    assert not SemanticDataset.objects.filter(workspace=workspace).exists()
