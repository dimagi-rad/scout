"""Regression for the raw OCS text -> saved topic dataset -> dashboard path (#406)."""

from unittest.mock import AsyncMock

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from psycopg import sql as psql

from apps.artifacts.models import Artifact, ArtifactType
from apps.artifacts.services.graph_manifest import sync_artifact_semantic_query_manifest
from apps.artifacts.services.graph_runtime import check_graph_artifact
from apps.chat.models import Thread
from apps.semantic.canvas import apply_operations, commit_canvas, resolve_thread_canvas
from apps.semantic.canvas import commit as canvas_commit_module
from apps.semantic.canvas import service as canvas_service
from apps.semantic.models import CubeSchema, SemanticDataset, SemanticModel
from apps.semantic.services import catalog as catalog_service
from apps.semantic.services import query as query_service
from apps.semantic.services.cube import generate_cube_schema, generate_cube_schema_yaml
from apps.semantic.services.cube_client import CubeClient
from mcp_server.context import QueryContext

TOPIC_SQL = """
SELECT message_id, session_id, created_at,
       CASE WHEN content ILIKE '%update%' THEN 'Account update'
            WHEN content ILIKE '%from home%' THEN 'Work from home'
            ELSE 'Other / unclassified' END AS topic
FROM raw_messages
WHERE role = 'user' AND content IS NOT NULL AND btrim(content) <> ''
"""


@pytest.mark.django_db(transaction=True)
def test_ocs_topic_dataset_can_be_committed_and_bound_to_dashboard(workspace, user, monkeypatch):
    model = SemanticModel.objects.create(workspace=workspace, name="OCS")
    messages = SemanticDataset.objects.create(
        semantic_model=model,
        workspace=workspace,
        name="raw_messages",
        table_name="raw_messages",
        schema_name="topic_test",
        primary_key="message_id",
    )
    catalog_service._sync_fields(
        messages,
        [
            {"name": "message_id", "type": "text"},
            {"name": "session_id", "type": "text"},
            {"name": "content", "type": "text"},
            {"name": "role", "type": "text"},
            {"name": "created_at", "type": "timestamp with time zone"},
        ],
        None,
    )
    thread = Thread.objects.create(workspace=workspace, user=user)
    canvas = resolve_thread_canvas(workspace, thread, user)

    def probe_columns(_workspace, compiled_sql):
        with connection.cursor() as cursor:
            cursor.execute(
                psql.SQL("SELECT * FROM ({}) topic_probe LIMIT 0").format(psql.SQL(compiled_sql))
            )
            return [
                {
                    "name": column.name,
                    "type": "timestamp with time zone" if column.type_code == 1184 else "text",
                }
                for column in cursor.description
            ]

    monkeypatch.setattr(canvas_service, "infer_custom_dataset_columns", probe_columns)
    monkeypatch.setattr(canvas_commit_module, "build_and_promote_cube_schema", lambda ws, m: None)

    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE raw_messages (message_id TEXT PRIMARY KEY, session_id TEXT, "
            "content TEXT, role TEXT, created_at TIMESTAMPTZ)"
        )
        cursor.executemany(
            "INSERT INTO raw_messages VALUES (%s, %s, %s, %s, %s)",
            [
                ("s1:0", "s1", "Please UPDATE my account", "user", "2026-07-01T10:00:00Z"),
                ("s1:1", "s1", "Please UPDATE my account", "user", "2026-07-01T10:01:00Z"),
                ("s2:0", "s2", "Can I work from home?", "user", "2026-07-02T10:00:00Z"),
                ("s2:1", "s2", "You can work from home", "assistant", "2026-07-02T10:01:00Z"),
                ("s3:0", "s3", "Something else entirely", "user", "2026-07-03T10:00:00Z"),
                ("s3:1", "s3", "   ", "user", "2026-07-03T10:01:00Z"),
            ],
        )
    try:
        result = apply_operations(
            canvas,
            [
                {
                    "op": "create",
                    "object_type": "custom_dataset",
                    "value": {
                        "name": "message_topics",
                        "label": "Message topics (keyword rules)",
                        "description": "Illustrative rules, not inferred NLP classifications.",
                        "definition_sql": TOPIC_SQL,
                        "primary_key": "message_id",
                    },
                }
            ],
            user,
        )
        assert result["diagnostics"] == []
        assert result["can_commit"] is True
        assert commit_canvas(canvas, user)["blocked"] is False

        dataset = model.datasets.get(name="message_topics")
        assert dataset.primary_key == "message_id"
        assert dataset.fields.get(name="topic").expression == "topic"
        assert dataset.fields.get(name="count").measure_type == "count"
        assert dataset.fields.get(name="created_at").field_type == "time_dimension"

        cube = next(c for c in generate_cube_schema(model)["cubes"] if c["name"] == dataset.name)
        assert cube["sql"] == dataset.metadata["cube_sql"]
        with connection.cursor() as cursor:
            cursor.execute(
                psql.SQL(
                    "SELECT topic, count(*) FROM ({}) topics GROUP BY topic ORDER BY topic"
                ).format(psql.SQL(cube["sql"]))
            )
            rows = [list(row) for row in cursor.fetchall()]
        assert rows == [["Account update", 2], ["Other / unclassified", 1], ["Work from home", 1]]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS pg_temp.raw_messages")

    CubeSchema.objects.create(
        workspace=workspace,
        semantic_model=model,
        filename="ocs_topics.yaml",
        content=generate_cube_schema_yaml(model),
        content_hash="ocs-topics-test",
    )
    monkeypatch.setattr(
        query_service,
        "load_workspace_context",
        AsyncMock(return_value=QueryContext(tenant_id="ocs-test", schema_name="topic_test")),
    )
    cube_query = AsyncMock(
        return_value={
            "columns": ["message_topics.topic", "message_topics.count"],
            "rows": rows,
            "row_count": len(rows),
        }
    )
    monkeypatch.setattr(CubeClient, "execute_query", cube_query)
    query_spec = {
        "measures": ["message_topics.count"],
        "dimensions": ["message_topics.topic"],
        "order_by": [{"field": "message_topics.count", "direction": "desc"}],
        "limit": 100,
    }
    artifact = Artifact.objects.create(
        workspace=workspace,
        created_by=user,
        artifact_type=ArtifactType.STORY,
        title="User message topics",
        data={
            "story_doc": {
                "schema_version": 1,
                "name": "User message topics",
                "blocks": [
                    {
                        "id": "q",
                        "type": "semantic_query",
                        "hidden": True,
                        "config": {"queries": {"topics": query_spec}},
                    },
                    {
                        "id": "topics",
                        "type": "graph",
                        "inputs": {"data": {"$ref": "q.topics"}},
                        "config": {
                            "title": "User messages by keyword-based topic",
                            "chart_type": "bar",
                            "x_key": "message_topics_topic",
                            "series": ["message_topics_count"],
                        },
                    },
                ],
            }
        },
    )
    manifest = sync_artifact_semantic_query_manifest(artifact)
    assert manifest["entries"][0]["members"] == ["message_topics.count", "message_topics.topic"]
    check = async_to_sync(check_graph_artifact)(artifact, user_id=str(user.id))
    assert check["success"] is True
    assert check["diagnostics"] == check["key_warnings"] == []
    assert check["queries"][0]["row_count"] == 3
    assert check["queries"][0]["result_keys"] == ["message_topics_count", "message_topics_topic"]
    assert cube_query.await_args.args[0]["measures"] == ["message_topics.count"]
    assert cube_query.await_args.args[0]["dimensions"] == ["message_topics.topic"]
    assert cube_query.await_args.kwargs["security_context"]["workspaceId"] == str(workspace.id)
