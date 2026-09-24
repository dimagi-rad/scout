"""Reviewed labels cannot follow reused message positions into a new export."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.db import connection

from apps.semantic.services import catalog
from apps.semantic.services.catalog import PhysicalTable
from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, WorkspaceViewSchema
from mcp_server.loaders.ocs_base import OCSExportError
from mcp_server.loaders.ocs_messages import map_session_messages
from mcp_server.services.materializer import _write_ocs_messages
from mcp_server.services.metadata import workspace_table_identity
from mcp_server.source_identity import source_identity

HISTORY = [
    {"role": "user", "content": "account help", "created_at": "2026-09-23T00:00:00Z"},
    {"role": "assistant", "content": "how can I help?", "created_at": "2026-09-23T00:00:01Z"},
    {"role": "user", "content": "account help", "created_at": "2026-09-23T00:00:00Z"},
]


@pytest.mark.parametrize(
    "changed",
    [
        HISTORY[1:],
        [HISTORY[1], HISTORY[0], HISTORY[2]],
        [{"role": "system", "content": "Summary"}, *HISTORY],
        [*HISTORY, {"role": "user", "content": "new message"}],
        [{**HISTORY[0], "content": "billing question"}, *HISTORY[1:]],
        [{**HISTORY[0], "tags": ["reviewed"]}, *HISTORY[1:]],
    ],
)
@pytest.mark.django_db
def test_changed_histories_do_not_inherit_saved_or_legacy_labels(changed):
    reviewed = map_session_messages("s1", HISTORY)
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA snapshot_identity_test")
        cursor.execute(
            "CREATE TEMP TABLE reviewed_labels (message_id text, message_version text, topic text)"
        )
        cursor.executemany(
            "INSERT INTO reviewed_labels VALUES (%s, %s, %s)",
            [
                (reviewed[0]["message_id"], reviewed[0]["message_version"], "Account"),
                ("s1:0", reviewed[0]["message_version"], "Legacy label"),
            ],
        )
    _write_ocs_messages(iter([(reviewed, 1)]), "snapshot_identity_test", connection)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM snapshot_identity_test.raw_messages m JOIN reviewed_labels l ON m.message_id=l.message_id AND m.message_version=l.message_version"
        )
        assert cursor.fetchone()[0] == 1
    current = map_session_messages("s1", changed)
    assert not {row["message_id"] for row in reviewed} & {row["message_id"] for row in current}
    _write_ocs_messages(iter([(current, 1)]), "snapshot_identity_test", connection)
    with connection.cursor() as cursor:
        # Even old ID-only CASE/join label mappings cannot attach to changed content.
        cursor.execute(
            "SELECT count(*), count(l.topic) FROM snapshot_identity_test.raw_messages m LEFT JOIN reviewed_labels l ON m.message_id=l.message_id"
        )
        assert cursor.fetchone() == (len(current), 0)


def test_identical_exports_are_stable_but_duplicate_messages_remain_distinct():
    rows = map_session_messages("s1", HISTORY)
    reordered_object_keys = [
        {key: message[key] for key in reversed(message)} for message in HISTORY
    ]
    assert rows == map_session_messages("s1", reordered_object_keys)
    assert len({row["message_id"] for row in rows}) == len(HISTORY)
    assert rows[0]["message_version"] == rows[2]["message_version"]
    assert rows[0]["message_id"] != rows[2]["message_id"]
    assert (
        rows[0]["snapshot_revision"] != map_session_messages("s2", HISTORY)[0]["snapshot_revision"]
    )


def test_ignored_export_fields_do_not_invalidate_stored_row_labels():
    original = map_session_messages("s1", HISTORY)
    exported = [{**message, "export_trace_id": "new-export"} for message in HISTORY]
    assert map_session_messages("s1", exported) == original


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_invalid_json_values_use_the_loader_error_contract(value):
    with pytest.raises(OCSExportError, match="invalid JSON"):
        map_session_messages("s1", [{"metadata": {"invalid": value}}])


@pytest.mark.parametrize("messages", [{"content": "bad"}, [None], ["bad"]])
def test_malformed_history_is_not_published(messages):
    with pytest.raises(OCSExportError):
        map_session_messages("s1", messages)


def test_legacy_ids_are_explicitly_unsafe_and_other_sources_are_not_guessed():
    legacy = source_identity("ocs", "raw_messages", [{"name": "message_id"}])
    assert legacy["kind"] == "snapshot_local"
    assert legacy["safe_for_reviewed_labels"] is False
    assert legacy["version"] == 1
    assert legacy["scope_columns"] == []
    assert source_identity("commcare", "raw_messages", []) is None
    assert source_identity("ocs", "user_table", []) is None


@pytest.mark.django_db
def test_identity_contract_survives_semantic_catalog_serialization(workspace, monkeypatch):
    columns = [
        {"name": name, "type": "text"}
        for name in ["message_id", "snapshot_revision", "message_version"]
    ]
    identity = source_identity("ocs", "raw_messages", columns)
    monkeypatch.setattr(
        catalog,
        "load_physical_tables",
        lambda ws: (
            "snapshots",
            [
                PhysicalTable(
                    name="raw_messages",
                    type="table",
                    description="Messages",
                    columns=columns,
                    primary_key="message_id",
                    identity=identity,
                )
            ],
        ),
    )
    model = catalog.ensure_semantic_model(workspace)
    dataset = catalog.serialize_catalog(model)["datasets"][0]
    assert dataset["metadata"]["identity"] == identity
    assert identity["safe_for_reviewed_labels"] is True
    assert identity["snapshot_column"] == "snapshot_revision"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("described", [False, True])
async def test_namespaced_views_keep_their_declared_source_identity(
    workspace, tenant, monkeypatch, legacy, described
):
    await Tenant.objects.filter(pk=tenant.pk).aupdate(provider="ocs")
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="identity_views",
        state=SchemaState.ACTIVE,
        view_sources={}
        if legacy
        else {
            "version": 1,
            "views": {
                "fitted_name": {"tenant_id": str(tenant.id), "source_table_name": "raw_messages"}
            },
        },
    )
    for attribute, value in {
        "load_workspace_context": SimpleNamespace(schema_name="identity_views"),
        "workspace_list_tables": [{"name": "fitted_name"}],
        "pipeline_describe_table": {
            "columns": [
                {"name": name, "type": "text"}
                for name in ["message_id", "snapshot_revision", "message_version"]
            ]
        }
        if described
        else None,
        "pipeline_table_primary_keys": {},
    }.items():
        monkeypatch.setattr(catalog, attribute, AsyncMock(return_value=value))
    _, tables = await catalog._load_physical_tables_async(workspace)
    identity = tables[0].identity
    if legacy or not described:
        assert identity["kind"] == "unknown"
        assert identity["safe_for_reviewed_labels"] is False
        assert "not evidence" in identity["label_policy"]
    if legacy:
        assert identity == await workspace_table_identity(
            workspace.id,
            SimpleNamespace(schema_name="identity_views"),
            "fitted_name",
            tables[0].columns,
        )
    elif described:
        assert identity["version"] == 2
        assert identity["kind"] == "snapshot_local"
        assert identity["safe_for_reviewed_labels"] is True
        assert identity["snapshot_column"] == "snapshot_revision"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapping", ["valid", "legacy", "unlisted", "other_tenant", "stale", "query_failed"]
)
async def test_mcp_view_identity_uses_only_authorized_publication_provenance(
    workspace, tenant, mapping, monkeypatch
):
    await Tenant.objects.filter(pk=tenant.pk).aupdate(provider="ocs")
    sources = {
        "version": 1,
        "views": {
            "fitted_name": {"tenant_id": str(tenant.id), "source_table_name": "raw_messages"}
        },
    }
    if mapping == "legacy":
        sources = {}
    elif mapping == "unlisted":
        sources["views"] = {}
    elif mapping == "other_tenant":
        sources["views"]["fitted_name"]["tenant_id"] = "not-in-workspace"
    published = [{"name": "fitted_name"}]
    if mapping == "stale":
        published.append({"name": "new_view_not_in_prior_map"})
    monkeypatch.setattr(
        "mcp_server.services.metadata.workspace_list_tables",
        AsyncMock(side_effect=TimeoutError("Managed database temporarily unavailable"))
        if mapping == "query_failed"
        else AsyncMock(return_value=published),
    )
    await WorkspaceViewSchema.objects.acreate(
        workspace=workspace,
        schema_name="identity_views",
        state=SchemaState.ACTIVE,
        view_sources=sources,
    )
    columns = [
        {"name": name, "type": "text"}
        for name in ["message_id", "session_id", "snapshot_revision", "message_version"]
    ]
    identity = await workspace_table_identity(
        workspace.id, SimpleNamespace(schema_name="identity_views"), "fitted_name", columns
    )
    if mapping == "valid":
        assert identity["kind"] == "snapshot_local"
        assert identity["safe_for_reviewed_labels"] is True
        assert identity["source_scope"] == "tenant"
        assert identity["scope_columns"] == ["session_id"]
    else:
        assert identity["kind"] == "unknown"
        assert identity["safe_for_reviewed_labels"] is False
