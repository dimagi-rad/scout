"""Provider event-time contracts use real PostgreSQL and retain raw evidence."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.db import connection

from apps.semantic.models import SemanticDataset, SemanticField, SemanticModel
from apps.semantic.services import catalog
from apps.semantic.services.field_sql import compile_dimension_sql
from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema
from mcp_server.event_time import event_time_sql, normalize_event_time
from mcp_server.services.materializer import _write_cases, _write_forms
from mcp_server.services.sql_validator import SQLValidationError, SQLValidator


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    [
        "2026-09-23",
        "2026-09-23T10:20:30Z",
        "2026-09-23 10:20:30.123456",
        "2026-09-23T10:20:30+05:30",
        "2026-09-23T10:20:30-04",
        "2024-02-29T10:20:30Z",
        "2026-02-29",
        "2026-13-01",
        "0000-01-01",
        "2026-09-23T24:00:00Z",
        "2026-09-23T10:20:60Z",
        "2026-09-23T10:20:30+25:00",
        "",
        None,
        "now",
        "tomorrow",
        "infinity",
        "09/23/2026",
        "not a date",
    ],
)
def test_python_and_sql_share_null_and_utc_semantics(value):
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL TIME ZONE 'America/New_York'")
        cursor.execute(
            "SELECT EXTRACT(EPOCH FROM ("
            + event_time_sql("value")
            + ")) FROM (SELECT %s::text AS value) source",
            [value],
        )
        epoch = cursor.fetchone()[0]
        expected = normalize_event_time(value)
        assert (float(epoch) if epoch is not None else None) == (
            expected.timestamp() if expected else None
        )


@pytest.mark.django_db
def test_materialized_hq_dates_are_typed_and_original_values_preserved():
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA event_time_test")
    _write_forms(
        iter(
            [
                (
                    [
                        {
                            "form_id": "f1",
                            "received_on": "2026-09-23T01:30:00+05:30",
                            "server_modified_on": "invalid",
                        },
                        {"form_id": "f2", "received_on": "", "server_modified_on": None},
                    ],
                    2,
                )
            ]
        ),
        "event_time_test",
        connection,
    )
    _write_cases(
        iter(
            [
                (
                    [
                        {
                            "case_id": "c1",
                            "date_opened": "2026-09-23",
                            "last_modified": "invalid",
                            "date_closed": "",
                        }
                    ],
                    1,
                )
            ]
        ),
        "event_time_test",
        connection,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT received_on, received_on_raw, server_modified_on, server_modified_on_raw FROM event_time_test.raw_forms WHERE form_id='f1'"
        )
        assert cursor.fetchone() == (
            datetime(2026, 9, 22, 20, tzinfo=UTC),
            "2026-09-23T01:30:00+05:30",
            None,
            "invalid",
        )
        cursor.execute(
            "SELECT date_opened, last_modified, last_modified_raw, date_closed FROM event_time_test.raw_cases"
        )
        assert cursor.fetchone() == (datetime(2026, 9, 23, tzinfo=UTC), None, "invalid", None)
        cursor.execute(
            "CREATE TEMP VIEW derived_times AS SELECT received_on FROM event_time_test.raw_forms"
        )
        cursor.execute("SELECT pg_typeof(received_on)::text FROM derived_times LIMIT 1")
        assert cursor.fetchone() == ("timestamp with time zone",)


@pytest.mark.django_db
def test_legacy_text_times_compile_as_time_but_arbitrary_dates_stay_text(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Typed times")
    dataset = SemanticDataset.objects.create(
        semantic_model=model, workspace=workspace, name="forms", table_name="raw_forms"
    )
    catalog._sync_fields(
        dataset,
        [
            {"name": "received_on", "type": "text", "event_time": {"naive_timezone": "UTC"}},
            {"name": "survey_date", "type": "text"},
            {"name": "received_on_raw", "type": "text"},
            {
                "name": "created_at",
                "type": "timestamp with time zone",
                "event_time": {"naive_timezone": "UTC"},
            },
        ],
        None,
    )
    field = dataset.fields.get(name="received_on")
    assert field.field_type == SemanticField.FieldType.TIME_DIMENSION
    assert field.data_type == "timestamp with time zone"
    assert field.metadata["cube_sql"] == event_time_sql("received_on")
    assert (
        "pg_catalog.pg_input_is_valid"
        in compile_dimension_sql(field.metadata["cube_sql"], columns={"received_on"}).lower()
    )
    for name in ["survey_date", "received_on_raw"]:
        assert dataset.fields.get(name=name).field_type == SemanticField.FieldType.DIMENSION
    assert "cube_sql" not in dataset.fields.get(name="created_at").metadata


@pytest.mark.parametrize(
    "type_sql",
    ["'custom_type'", "'text'", "column_name", "'timestamp'", "'timestamp with time zone'::text"],
)
def test_input_validation_cannot_invoke_arbitrary_type_input_functions(type_sql):
    with pytest.raises(SQLValidationError):
        SQLValidator().validate(f"SELECT pg_input_is_valid(value, {type_sql})")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "table", "column", "declared"),
    [
        ("commcare", "raw_forms", "received_on", True),
        ("commcare", "raw_forms", "survey_date", False),
        ("ocs", "raw_messages", "created_at", True),
        ("commcare_connect", "raw_visits", "visit_date", True),
        ("ocs", "raw_forms", "received_on", False),
    ],
)
@pytest.mark.parametrize("view_schema", [False, True])
async def test_times_follow_provider_and_published_provenance(
    workspace, tenant, monkeypatch, provider, table, column, declared, view_schema
):
    await Tenant.objects.filter(pk=tenant.pk).aupdate(provider=provider)
    name = "fitted_view_name" if view_schema else table
    if view_schema:
        await WorkspaceViewSchema.objects.acreate(
            workspace=workspace,
            schema_name="time_views",
            state=SchemaState.ACTIVE,
            view_sources={
                "version": 1,
                "views": {name: {"tenant_id": str(tenant.pk), "source_table_name": table}},
            },
        )
        schema_name = "time_views"
    else:
        await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="time_tenant", state=SchemaState.ACTIVE
        )
        schema_name = "time_tenant"
    for attribute, value in {
        "load_workspace_context": SimpleNamespace(schema_name=schema_name),
        "workspace_list_tables": [{"name": name}],
        "pipeline_list_tables": [{"name": name}],
        "pipeline_describe_table": {"columns": [{"name": column, "type": "text"}]},
        "pipeline_table_primary_keys": {},
        "aresolve_pipeline_config": None,
        "aget_tenant_metadata": None,
    }.items():
        monkeypatch.setattr(catalog, attribute, AsyncMock(return_value=value))
    _, tables = await catalog._load_physical_tables_async(workspace)
    assert ("event_time" in tables[0].columns[0]) is declared
