"""Tests for data dictionary table annotation storage (arch #262).

Covers:
- 01#5: TableKnowledge keyed by a stable logical table name (bare table name),
  not the physical schema-qualified name, so annotations survive a schema
  refresh that mints a new physical schema name.
- 05#0: Annotation autosave must use true partial-update semantics: list/dict
  fields absent from the payload (e.g. related_tables) must NOT be clobbered.
"""

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


@pytest.fixture
def patched_table_data():
    """Patch _get_table_data so the view believes the table exists without a managed DB."""
    table_data = {
        "schema": "commcare_testdomain_r1a2b3c4",
        "name": "cases",
        "type": "table",
        "columns": [],
        "primary_key": [],
    }
    with mock.patch(
        "apps.workspaces.api.views.TableDetailView._get_table_data",
        return_value=table_data,
    ):
        yield table_data


def _put(auth_client, workspace, qualified_name, payload):
    return auth_client.put(
        f"/api/workspaces/{workspace.id}/data-dictionary/tables/{qualified_name}/",
        payload,
        format="json",
    )


# ── 01#5 ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_annotation_stored_under_logical_table_name(
    auth_client, workspace, active_schema, patched_table_data
):
    """PUT must persist TableKnowledge keyed by the bare logical table name,
    not the physical schema-qualified name."""
    resp = _put(
        auth_client,
        workspace,
        "commcare_testdomain_r1a2b3c4.cases",
        {"description": "Case records"},
    )
    assert resp.status_code == 200

    # The stored key must be the bare table name so it survives a refresh.
    assert TableKnowledge.objects.filter(workspace=workspace, table_name="cases").exists()
    # And it must NOT be stored under the physical schema-qualified name.
    assert not TableKnowledge.objects.filter(
        workspace=workspace, table_name="commcare_testdomain_r1a2b3c4.cases"
    ).exists()


@pytest.mark.django_db
def test_annotation_survives_schema_refresh(
    auth_client, workspace, tenant, active_schema, patched_table_data
):
    """An annotation saved under one physical schema must still be returned
    after a refresh mints a new physical schema name."""
    resp = _put(
        auth_client,
        workspace,
        "commcare_testdomain_r1a2b3c4.cases",
        {"description": "Case records", "owner": "Data Team"},
    )
    assert resp.status_code == 200

    # Simulate a refresh: the active schema name changes.
    active_schema.schema_name = "commcare_testdomain_rDEADBEEF"
    active_schema.save(update_fields=["schema_name"])

    # GET under the NEW physical schema name should still find the annotation.
    new_table_data = {
        "schema": "commcare_testdomain_rDEADBEEF",
        "name": "cases",
        "type": "table",
        "columns": [],
        "primary_key": [],
    }
    with mock.patch(
        "apps.workspaces.api.views.TableDetailView._get_table_data",
        return_value=new_table_data,
    ):
        resp = auth_client.get(
            f"/api/workspaces/{workspace.id}"
            f"/data-dictionary/tables/commcare_testdomain_rDEADBEEF.cases/"
        )
    assert resp.status_code == 200
    assert resp.data["annotation"]["description"] == "Case records"
    assert resp.data["annotation"]["owner"] == "Data Team"


# ── 05#0 ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_autosave_does_not_clobber_related_tables(
    auth_client, workspace, active_schema, patched_table_data
):
    """A debounced autosave payload that omits related_tables must NOT wipe a
    previously curated related_tables annotation."""
    # Seed a curated annotation with related_tables under the logical name.
    TableKnowledge.objects.create(
        workspace=workspace,
        table_name="cases",
        description="Case records",
        related_tables=[{"table": "forms", "join_hint": "cases.id = forms.case_id"}],
    )

    # Frontend autosave payload (note: no related_tables, no description).
    resp = _put(
        auth_client,
        workspace,
        "commcare_testdomain_r1a2b3c4.cases",
        {
            "use_cases": "Track case lifecycle",
            "data_quality_notes": "",
            "refresh_frequency": "daily",
            "owner": "",
            "column_notes": {},
        },
    )
    assert resp.status_code == 200

    tk = TableKnowledge.objects.get(workspace=workspace, table_name="cases")
    # related_tables must be preserved, not clobbered to [].
    assert tk.related_tables == [{"table": "forms", "join_hint": "cases.id = forms.case_id"}]
    # The field that WAS present should be updated.
    assert tk.refresh_frequency == "daily"


@pytest.mark.django_db
def test_explicit_related_tables_is_still_updated(
    auth_client, workspace, active_schema, patched_table_data
):
    """When related_tables IS present in the payload, it must be applied."""
    TableKnowledge.objects.create(
        workspace=workspace,
        table_name="cases",
        description="Case records",
        related_tables=[{"table": "old", "join_hint": "x"}],
    )

    resp = _put(
        auth_client,
        workspace,
        "commcare_testdomain_r1a2b3c4.cases",
        {"related_tables": [{"table": "forms", "join_hint": "cases.id = forms.case_id"}]},
    )
    assert resp.status_code == 200

    tk = TableKnowledge.objects.get(workspace=workspace, table_name="cases")
    assert tk.related_tables == [{"table": "forms", "join_hint": "cases.id = forms.case_id"}]


@pytest.mark.django_db
def test_autosave_does_not_clobber_column_notes_when_absent(
    auth_client, workspace, active_schema, patched_table_data
):
    """column_notes (a dict field) absent from the payload must be preserved."""
    TableKnowledge.objects.create(
        workspace=workspace,
        table_name="cases",
        description="Case records",
        column_notes={"status": "Values: open, closed"},
    )

    resp = _put(
        auth_client,
        workspace,
        "commcare_testdomain_r1a2b3c4.cases",
        {"owner": "Data Team"},
    )
    assert resp.status_code == 200

    tk = TableKnowledge.objects.get(workspace=workspace, table_name="cases")
    assert tk.column_notes == {"status": "Values: open, closed"}
    assert tk.owner == "Data Team"
