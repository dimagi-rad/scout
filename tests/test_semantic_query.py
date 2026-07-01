from unittest.mock import MagicMock

import pytest

from apps.semantic.models import (
    CubeSchema,
    CustomDataset,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from apps.semantic.services import catalog as catalog_service
from apps.semantic.services import cube_schema as cube_schema_service
from apps.semantic.services import query as query_service
from apps.semantic.services.catalog import PhysicalTable
from apps.semantic.services.cube import generate_cube_schema
from apps.semantic.services.cube_schema import (
    KEEP_INACTIVE_CUBE_SCHEMAS,
    CubeSchemaBuildError,
    build_cube_security_context,
)
from mcp_server.context import QueryContext
from mcp_server.pipeline_registry import RelationshipConfig


@pytest.fixture
def semantic_model(workspace):
    model = SemanticModel.objects.create(workspace=workspace, name="Test Semantic Model")
    visits = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="visits",
        label="Visits",
        table_name="raw_visits",
        schema_name="tenant_schema",
    )
    SemanticField.objects.create(
        dataset=visits,
        name="count",
        label="Count",
        field_type=SemanticField.FieldType.MEASURE,
        data_type="integer",
        expression="*",
        measure_type=SemanticField.MeasureType.COUNT,
    )
    SemanticField.objects.create(
        dataset=visits,
        name="username",
        label="Username",
        field_type=SemanticField.FieldType.DIMENSION,
        data_type="text",
        expression="username",
    )
    SemanticField.objects.create(
        dataset=visits,
        name="visit_date",
        label="Visit Date",
        field_type=SemanticField.FieldType.TIME_DIMENSION,
        data_type="timestamp with time zone",
        expression="visit_date",
    )
    SemanticField.objects.create(
        dataset=visits,
        name="sum_amount",
        label="Total Amount",
        field_type=SemanticField.FieldType.MEASURE,
        data_type="numeric",
        expression="amount",
        measure_type=SemanticField.MeasureType.SUM,
    )
    CubeSchema.objects.create(
        workspace=workspace,
        semantic_model=model,
        filename="workspace_test.yaml",
        content="cubes: []\n",
        content_hash="testhash",
    )
    return model


def test_compile_semantic_query_from_members(monkeypatch, workspace, semantic_model):
    monkeypatch.setattr(query_service, "get_active_semantic_model", lambda _workspace: semantic_model)

    compiled = query_service._compile_semantic_query(
        workspace,
        {
            "measures": ["visits.count"],
            "dimensions": ["visits.username"],
            "time_dimension": "visits.visit_date",
            "granularity": "day",
            "filters": [
                {
                    "field": "visits.visit_date",
                    "operator": "inDateRange",
                    "value": ["2026-06-22", "2026-06-28"],
                }
            ],
            "order_by": [{"field": "visits.count", "direction": "desc"}],
            "limit": 50,
        },
    )

    assert compiled["members"] == ["visits.visit_date", "visits.username", "visits.count"]
    assert compiled["cube_query"] == {
        "measures": ["visits.count"],
        "dimensions": ["visits.username"],
        "filters": [
            {
                "member": "visits.visit_date",
                "operator": "inDateRange",
                "values": ["2026-06-22", "2026-06-28"],
            }
        ],
        "limit": 50,
        "timeDimensions": [{"dimension": "visits.visit_date", "granularity": "day"}],
        "order": [["visits.count", "desc"]],
    }


def test_compile_time_granularity_does_not_duplicate_params(
    monkeypatch, workspace, semantic_model
):
    monkeypatch.setattr(query_service, "get_active_semantic_model", lambda _workspace: semantic_model)

    compiled = query_service._compile_semantic_query(
        workspace,
        {
            "measures": ["visits.count"],
            "time_dimension": "visits.visit_date",
            "granularity": "week",
            "order_by": [{"field": "visits.visit_date", "direction": "desc"}],
            "limit": 100,
        },
    )

    assert compiled["cube_query"] == {
        "measures": ["visits.count"],
        "limit": 100,
        "timeDimensions": [{"dimension": "visits.visit_date", "granularity": "week"}],
        "order": [["visits.visit_date", "desc"]],
    }


def test_compile_rejects_unknown_member(monkeypatch, workspace, semantic_model):
    monkeypatch.setattr(query_service, "get_active_semantic_model", lambda _workspace: semantic_model)

    with pytest.raises(query_service.SemanticQueryError, match="Unknown semantic field"):
        query_service._compile_semantic_query(
            workspace,
            {"measures": ["visits.missing"]},
        )


def test_compile_rejects_cross_dataset_query(monkeypatch, workspace, semantic_model):
    users = SemanticDataset.objects.create(
        semantic_model=semantic_model,
        workspace=workspace,
        name="users",
        label="Users",
        table_name="raw_users",
        schema_name="tenant_schema",
    )
    SemanticField.objects.create(
        dataset=users,
        name="username",
        label="Username",
        field_type=SemanticField.FieldType.DIMENSION,
        data_type="text",
        expression="username",
    )
    monkeypatch.setattr(query_service, "get_active_semantic_model", lambda _workspace: semantic_model)

    with pytest.raises(query_service.SemanticQueryError, match="one dataset"):
        query_service._compile_semantic_query(
            workspace,
            {
                "measures": ["visits.count"],
                "dimensions": ["users.username"],
            },
        )


def test_compile_uses_active_semantic_model_without_refresh(
    monkeypatch,
    workspace,
    semantic_model,
):
    def fail_refresh(_workspace):
        raise AssertionError("semantic query execution must not refresh the semantic catalog")

    monkeypatch.setattr(catalog_service, "load_physical_tables", fail_refresh)

    compiled = query_service._compile_semantic_query(
        workspace,
        {"measures": ["visits.count"], "limit": 10},
    )

    assert compiled["cube_query"] == {"measures": ["visits.count"], "limit": 10}


def test_compile_requires_prebuilt_active_semantic_model_without_refresh(
    monkeypatch,
    workspace,
):
    def fail_refresh(_workspace):
        raise AssertionError("semantic query execution must not refresh the semantic catalog")

    monkeypatch.setattr(catalog_service, "load_physical_tables", fail_refresh)

    with pytest.raises(
        catalog_service.SemanticCatalogUnavailable,
        match="No active semantic model",
    ):
        query_service._compile_semantic_query(
            workspace,
            {"measures": ["visits.count"], "limit": 10},
        )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_semantic_query_executes_via_cube(monkeypatch, workspace, semantic_model):
    captured = {}

    class FakeCubeClient:
        async def execute_query(self, cube_query, *, security_context):
            captured["cube_query"] = cube_query
            captured["security_context"] = security_context
            return {
                "columns": ["visits.count"],
                "rows": [[3]],
                "row_count": 1,
            }

    async def fake_context(_workspace_id):
        return QueryContext(
            tenant_id=str(workspace.id),
            schema_name="tenant_schema",
            connection_params={},
        )

    monkeypatch.setattr(query_service, "get_active_semantic_model", lambda _workspace: semantic_model)
    monkeypatch.setattr(query_service, "CubeClient", FakeCubeClient)
    monkeypatch.setattr(query_service, "load_workspace_context", fake_context)

    result = await query_service.run_semantic_query(
        workspace,
        {"measures": ["visits.count"], "limit": 10},
        user_id="user-1",
    )

    assert result["columns"] == ["visits.count"]
    assert result["rows"] == [[3]]
    assert captured["cube_query"] == {"measures": ["visits.count"], "limit": 10}
    assert captured["security_context"]["workspaceId"] == str(workspace.id)
    assert captured["security_context"]["userId"] == "user-1"
    assert captured["security_context"]["readonlyRole"] == "tenant_schema_ro"


def test_generate_cube_schema_from_semantic_model(semantic_model):
    schema = generate_cube_schema(semantic_model)

    assert schema["model"]["version"] == 1
    cube = schema["cubes"][0]
    assert cube["name"] == "visits"
    # Unqualified on purpose: the schema resolves via per-query search_path.
    assert cube["sql_table"] == '"raw_visits"'
    assert {"name": "count", "type": "count"} in cube["measures"]
    assert {
        "name": "username",
        "sql": '{CUBE}."username"',
        "type": "string",
    } in cube["dimensions"]
    assert {
        "name": "visit_date",
        "sql": '{CUBE}."visit_date"',
        "type": "time",
    } in cube["dimensions"]


def test_generate_cube_schema_renders_custom_dataset_as_sql(workspace, semantic_model):
    custom = CustomDataset.objects.create(
        workspace=workspace,
        name="large_visits",
        definition_sql="select username from raw_visits",
    )
    dataset = SemanticDataset.objects.create(
        semantic_model=semantic_model,
        workspace=workspace,
        custom_dataset=custom,
        name="large_visits",
        label="Large Visits",
        source_kind=SemanticDataset.SourceKind.CUSTOM,
        table_name="large_visits",
        metadata={"cube_sql": 'select username from "raw_visits"'},
    )
    SemanticField.objects.create(
        dataset=dataset,
        name="username",
        label="Username",
        field_type=SemanticField.FieldType.DIMENSION,
        data_type="text",
        expression="username",
    )

    cube = next(
        cube
        for cube in generate_cube_schema(semantic_model)["cubes"]
        if cube["name"] == "large_visits"
    )

    assert cube["sql"] == 'select username from "raw_visits"'
    assert "sql_table" not in cube


def test_ensure_semantic_model_syncs_valid_custom_dataset(monkeypatch, workspace):
    monkeypatch.setattr(
        catalog_service,
        "load_physical_tables",
        lambda _workspace: (
            "tenant_schema",
            [
                PhysicalTable(
                    name="raw_visits",
                    type="table",
                    description="Visits",
                    columns=[{"name": "username", "type": "text"}],
                )
            ],
        ),
    )
    monkeypatch.setattr(
        catalog_service,
        "infer_custom_dataset_columns",
        lambda _workspace, _sql: [{"name": "username", "type": "text"}],
    )
    CustomDataset.objects.create(
        workspace=workspace,
        name="visit_users",
        definition_sql="select username from raw_visits",
    )

    model = catalog_service.ensure_semantic_model(workspace)
    custom_dataset = model.datasets.get(name="visit_users")

    assert custom_dataset.source_kind == SemanticDataset.SourceKind.CUSTOM
    assert custom_dataset.metadata["cube_sql"] == 'select username from "raw_visits"'
    assert custom_dataset.fields.filter(name="username", is_visible=True).exists()
    assert model.datasets.filter(name="raw_visits", is_visible=True).exists()


def test_invalid_custom_dataset_is_hidden_without_removing_physical(monkeypatch, workspace):
    monkeypatch.setattr(
        catalog_service,
        "load_physical_tables",
        lambda _workspace: (
            "tenant_schema",
            [
                PhysicalTable(
                    name="raw_visits",
                    type="table",
                    description="Visits",
                    columns=[{"name": "username", "type": "text"}],
                )
            ],
        ),
    )
    custom = CustomDataset.objects.create(
        workspace=workspace,
        name="bad_dataset",
        definition_sql="drop table raw_visits",
    )

    model = catalog_service.ensure_semantic_model(workspace)
    custom.refresh_from_db()

    assert custom.status == CustomDataset.Status.ERROR
    assert model.datasets.filter(
        name="raw_visits",
        source_kind=SemanticDataset.SourceKind.PHYSICAL,
        is_visible=True,
    ).exists()
    assert not model.datasets.filter(name="bad_dataset", is_visible=True).exists()


def _physical_visits_and_users():
    return [
        PhysicalTable(
            name="raw_visits",
            type="table",
            description="Visits",
            columns=[
                {"name": "visit_id", "type": "bigint"},
                {"name": "username", "type": "text"},
                {"name": "amount", "type": "numeric"},
            ],
            primary_key="visit_id",
        ),
        PhysicalTable(
            name="raw_users",
            type="table",
            description="Users",
            columns=[{"name": "username", "type": "text"}],
            primary_key="username",
        ),
    ]


def _fake_registry(relationships):
    pipeline = MagicMock()
    pipeline.relationships = relationships
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    return registry


_VISITS_TO_USERS = RelationshipConfig(
    from_table="raw_visits",
    from_column="username",
    to_table="raw_users",
    to_column="username",
    description="Visits reference the FLW",
)


def test_ensure_semantic_model_sets_primary_key_and_skips_id_measures(monkeypatch, workspace):
    monkeypatch.setattr(
        catalog_service,
        "load_physical_tables",
        lambda _workspace: ("tenant_schema", _physical_visits_and_users()),
    )
    monkeypatch.setattr(catalog_service, "get_registry", lambda: _fake_registry([]))

    model = catalog_service.ensure_semantic_model(workspace)

    visits = model.datasets.get(name="raw_visits")
    assert visits.primary_key == "visit_id"
    assert not visits.fields.filter(name="sum_visit_id").exists()
    assert not visits.fields.filter(name="avg_visit_id").exists()
    assert visits.fields.filter(name="sum_amount", is_visible=True).exists()


def test_generate_cube_schema_marks_primary_key_dimension(monkeypatch, workspace):
    monkeypatch.setattr(
        catalog_service,
        "load_physical_tables",
        lambda _workspace: ("tenant_schema", _physical_visits_and_users()),
    )
    monkeypatch.setattr(catalog_service, "get_registry", lambda: _fake_registry([]))
    model = catalog_service.ensure_semantic_model(workspace)

    cube = next(c for c in generate_cube_schema(model)["cubes"] if c["name"] == "raw_visits")

    pk_dim = next(d for d in cube["dimensions"] if d["name"] == "visit_id")
    assert pk_dim["primary_key"] is True
    assert pk_dim["public"] is True
    username_dim = next(d for d in cube["dimensions"] if d["name"] == "username")
    assert "primary_key" not in username_dim


def test_ensure_semantic_model_builds_relationships_from_pipeline(monkeypatch, workspace):
    monkeypatch.setattr(
        catalog_service,
        "load_physical_tables",
        lambda _workspace: ("tenant_schema", _physical_visits_and_users()),
    )
    monkeypatch.setattr(catalog_service, "get_registry", lambda: _fake_registry([_VISITS_TO_USERS]))

    model = catalog_service.ensure_semantic_model(workspace)

    relationship = SemanticRelationship.objects.get(workspace=workspace)
    assert relationship.from_dataset.name == "raw_visits"
    assert relationship.to_dataset.name == "raw_users"
    assert relationship.relationship_type == SemanticRelationship.RelationshipType.MANY_TO_ONE
    assert relationship.join_expression == "{raw_visits.username} = {raw_users.username}"
    assert relationship.metadata["generated"] is True

    cube = next(c for c in generate_cube_schema(model)["cubes"] if c["name"] == "raw_visits")
    assert cube["joins"] == [
        {
            "name": "raw_users",
            "relationship": "many_to_one",
            "sql": "{raw_visits.username} = {raw_users.username}",
        }
    ]


def test_relationship_sync_drops_stale_generated_rows(monkeypatch, workspace):
    monkeypatch.setattr(
        catalog_service,
        "load_physical_tables",
        lambda _workspace: ("tenant_schema", _physical_visits_and_users()),
    )
    monkeypatch.setattr(catalog_service, "get_registry", lambda: _fake_registry([_VISITS_TO_USERS]))
    model = catalog_service.ensure_semantic_model(workspace)
    hand_authored = SemanticRelationship.objects.create(
        workspace=workspace,
        name="manual_link",
        from_dataset=model.datasets.get(name="raw_visits"),
        to_dataset=model.datasets.get(name="raw_users"),
        join_expression="{raw_visits.username} = {raw_users.username}",
    )

    monkeypatch.setattr(catalog_service, "get_registry", lambda: _fake_registry([]))
    catalog_service.ensure_semantic_model(workspace)

    remaining = SemanticRelationship.objects.filter(workspace=workspace)
    assert list(remaining.values_list("id", flat=True)) == [hand_authored.id]


def test_relationships_match_namespaced_views_within_prefix(monkeypatch, workspace):
    tables = [
        PhysicalTable(
            name="t1__raw_visits",
            type="view",
            description="",
            columns=[{"name": "username", "type": "text"}],
        ),
        PhysicalTable(
            name="t1__raw_users",
            type="view",
            description="",
            columns=[{"name": "username", "type": "text"}],
        ),
        PhysicalTable(
            name="t2__raw_users",
            type="view",
            description="",
            columns=[{"name": "username", "type": "text"}],
        ),
    ]
    monkeypatch.setattr(
        catalog_service, "load_physical_tables", lambda _workspace: ("ws_schema", tables)
    )
    monkeypatch.setattr(catalog_service, "get_registry", lambda: _fake_registry([_VISITS_TO_USERS]))

    catalog_service.ensure_semantic_model(workspace)

    relationship = SemanticRelationship.objects.get(workspace=workspace)
    assert relationship.from_dataset.name == "t1__raw_visits"
    assert relationship.to_dataset.name == "t1__raw_users"


@pytest.fixture
def no_close_old_connections(monkeypatch):
    """build_and_promote_cube_schema closes worker-thread DB connections; inside
    pytest-django's per-test transaction that would kill the test connection."""
    monkeypatch.setattr(cube_schema_service, "close_old_connections", lambda: None)


class _FailingValidatorClient:
    async def validate_schema(self, content):
        return {"valid": False, "errors": ["boom"]}


class _OkCubeClient:
    async def validate_schema(self, content):
        return {"valid": True, "errors": []}

    async def invalidate_schema_cache(self, *, security_context):
        return None


async def _fake_workspace_context(workspace_id):
    return QueryContext(
        tenant_id=str(workspace_id),
        schema_name="tenant_schema",
        connection_params={},
    )


def test_failed_build_keeps_last_known_good_readable(
    monkeypatch, workspace, semantic_model, no_close_old_connections
):
    monkeypatch.setattr(cube_schema_service, "CubeClient", _FailingValidatorClient)

    with pytest.raises(CubeSchemaBuildError):
        cube_schema_service.build_and_promote_cube_schema(workspace, model=semantic_model)

    semantic_model.refresh_from_db()
    # The previous ACTIVE schema still serves, so reads must stay up.
    assert semantic_model.status == SemanticModel.Status.ACTIVE
    assert semantic_model.metadata["last_build"]["ok"] is False
    active = CubeSchema.objects.get(workspace=workspace, status=CubeSchema.Status.ACTIVE)
    assert active.content_hash == "testhash"
    assert CubeSchema.objects.filter(workspace=workspace, status=CubeSchema.Status.ERROR).exists()


def test_failed_build_without_active_schema_marks_model_error(
    monkeypatch, workspace, semantic_model, no_close_old_connections
):
    CubeSchema.objects.filter(workspace=workspace).delete()
    monkeypatch.setattr(cube_schema_service, "CubeClient", _FailingValidatorClient)

    with pytest.raises(CubeSchemaBuildError):
        cube_schema_service.build_and_promote_cube_schema(workspace, model=semantic_model)

    semantic_model.refresh_from_db()
    assert semantic_model.status == SemanticModel.Status.ERROR
    assert semantic_model.metadata["last_build"]["ok"] is False


def test_promote_prunes_old_inactive_rows_and_records_last_build(
    monkeypatch, workspace, semantic_model, no_close_old_connections
):
    for i in range(KEEP_INACTIVE_CUBE_SCHEMAS + 3):
        CubeSchema.objects.create(
            workspace=workspace,
            semantic_model=semantic_model,
            filename=f"old_{i}.yaml",
            content="cubes: []\n",
            content_hash=f"old{i}",
            status=CubeSchema.Status.DRAFT,
        )
    monkeypatch.setattr(cube_schema_service, "CubeClient", _OkCubeClient)
    monkeypatch.setattr(cube_schema_service, "load_workspace_context", _fake_workspace_context)

    promoted = cube_schema_service.build_and_promote_cube_schema(workspace, model=semantic_model)

    assert promoted.status == CubeSchema.Status.ACTIVE
    semantic_model.refresh_from_db()
    assert semantic_model.metadata["last_build"]["ok"] is True
    inactive = CubeSchema.objects.filter(workspace=workspace).exclude(
        status=CubeSchema.Status.ACTIVE
    )
    assert inactive.count() == KEEP_INACTIVE_CUBE_SCHEMAS


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_semantic_query_flags_truncation_at_limit(monkeypatch, workspace, semantic_model):
    class FullPageCubeClient:
        async def execute_query(self, cube_query, *, security_context):
            limit = cube_query["limit"]
            return {
                "columns": ["visits.count"],
                "rows": [[i] for i in range(limit)],
                "row_count": limit,
            }

    monkeypatch.setattr(
        query_service, "get_active_semantic_model", lambda _workspace: semantic_model
    )
    monkeypatch.setattr(query_service, "CubeClient", FullPageCubeClient)
    monkeypatch.setattr(query_service, "load_workspace_context", _fake_workspace_context)

    result = await query_service.run_semantic_query(
        workspace, {"measures": ["visits.count"], "limit": 5}
    )
    assert result["truncated"] is True

    class SparseCubeClient:
        async def execute_query(self, cube_query, *, security_context):
            return {"columns": ["visits.count"], "rows": [[1]], "row_count": 1}

    monkeypatch.setattr(query_service, "CubeClient", SparseCubeClient)
    result = await query_service.run_semantic_query(
        workspace,
        {"measures": ["visits.count"], "limit": query_service.MAX_SEMANTIC_LIMIT},
    )
    assert result["truncated"] is False


def test_cube_security_context_is_workspace_and_schema_scoped(workspace, semantic_model):
    cube_schema = CubeSchema.objects.get(semantic_model=semantic_model)
    ctx = QueryContext(
        tenant_id=str(workspace.id),
        schema_name="tenant_schema",
        connection_params={},
    )

    security_context = build_cube_security_context(
        workspace,
        semantic_model,
        cube_schema,
        ctx,
        user_id="user-1",
    )

    assert security_context["workspaceId"] == str(workspace.id)
    assert security_context["semanticModelId"] == str(semantic_model.id)
    assert security_context["cubeSchemaHash"] == "testhash"
    assert security_context["schemaName"] == "tenant_schema"
    assert security_context["readonlyRole"] == "tenant_schema_ro"
    assert security_context["userId"] == "user-1"
