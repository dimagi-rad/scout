"""Data Dictionary view perf (arch #254, finding 10#2).

DataDictionaryView._get_from_pipeline issued:
  - one TableKnowledge.objects.get per table (N+1; one filter would do),
  - two separate fresh managed-DB connections per request, and
  - ran async_to_sync inside a sync DRF view (a new event loop per request).

This verifies: TableKnowledge is fetched in a single query, the managed DB is
opened once, and no async_to_sync remains in the view module.
"""

import ast
from pathlib import Path
from unittest import mock

import pytest
from rest_framework.test import APIClient

from apps.knowledge.models import TableKnowledge
from apps.workspaces.models import SchemaState, TenantSchema


@pytest.fixture
def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def active_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant,
        schema_name="commcare_testdomain_r1a2b3c4",
        state=SchemaState.ACTIVE,
    )


def _fake_columns():
    # Mapping of table_name -> column dicts, as _get_all_columns returns.
    return {f"table_{i}": [{"name": "id", "data_type": "uuid"}] for i in range(5)}


def _fake_tables():
    return [{"name": f"table_{i}", "type": "table"} for i in range(5)]


@pytest.mark.django_db
def test_data_dictionary_batches_table_knowledge(auth_client, workspace, active_schema, user):
    """One TableKnowledge query for the whole page, not one per table (10#2).

    The N+1 was ``TableKnowledge.objects.get`` per table; a single batched
    ``.filter`` must replace it so the per-table ``.get`` disappears entirely.
    """
    for i in range(5):
        TableKnowledge.objects.create(
            workspace=workspace, table_name=f"table_{i}", description=f"d{i}", updated_by=user
        )

    real_get = TableKnowledge.objects.get
    get_calls = {"n": 0}

    def counting_get(*args, **kwargs):
        get_calls["n"] += 1
        return real_get(*args, **kwargs)

    with (
        mock.patch(
            "apps.workspaces.api.views._sync_pipeline_list_tables",
            return_value=_fake_tables(),
        ),
        # The view now opens the managed DB once via get_managed_db_connection;
        # stub the per-conn reads so no real DB is needed.
        mock.patch(
            "apps.workspaces.api.views.get_managed_db_connection",
            return_value=mock.MagicMock(),
        ),
        mock.patch(
            "apps.workspaces.api.views._live_tables_from_conn",
            return_value={f"table_{i}" for i in range(5)},
        ),
        mock.patch(
            "apps.workspaces.api.views._columns_from_conn",
            return_value=_fake_columns(),
        ),
        mock.patch.object(TableKnowledge.objects, "get", side_effect=counting_get),
    ):
        resp = auth_client.get(f"/api/workspaces/{workspace.id}/data-dictionary/")

    assert resp.status_code == 200
    tables = resp.json()["tables"]
    # All five annotations are present...
    annotated = [t for t in tables.values() if t.get("annotation")]
    assert len(annotated) == 5
    # ...with NO per-table .get (the N+1) — they came from one batched query.
    assert get_calls["n"] == 0, "per-table TableKnowledge.get is the N+1 being removed"


@pytest.mark.django_db
def test_data_dictionary_opens_managed_db_once(auth_client, workspace, active_schema, user):
    """The request opens a single managed-DB connection, not two (10#2)."""
    with (
        mock.patch(
            "apps.workspaces.api.views._sync_pipeline_list_tables",
            return_value=_fake_tables(),
        ),
        mock.patch(
            "apps.workspaces.api.views.get_managed_db_connection",
            return_value=mock.MagicMock(),
        ) as conn_mock,
        mock.patch(
            "apps.workspaces.api.views._live_tables_from_conn",
            return_value=set(),
        ),
        mock.patch(
            "apps.workspaces.api.views._columns_from_conn",
            return_value={},
        ),
    ):
        resp = auth_client.get(f"/api/workspaces/{workspace.id}/data-dictionary/")

    assert resp.status_code == 200
    assert conn_mock.call_count == 1, "managed DB must be opened once, not per helper"


def test_data_dictionary_view_has_no_async_to_sync():
    """The sync DRF view must not spin up an event loop per request (10#2)."""
    src = Path("apps/workspaces/api/views.py").read_text()
    tree = ast.parse(src)
    names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "async_to_sync"
    }
    assert not names, "async_to_sync must be removed from the data dictionary view (10#2)"
