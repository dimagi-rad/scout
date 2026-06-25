"""Tests for the TableKnowledge re-keying data migration (arch #262, 01#5).

Exercises the forward migration's data transform: qualified rows are re-keyed
to their bare logical name, and collisions with existing logical rows are merged
deterministically (qualified human-curated values fill gaps in the existing row).
"""

import importlib

import pytest
from django.apps import apps as django_apps

from apps.knowledge.models import TableKnowledge

_migration = importlib.import_module(
    "apps.knowledge.migrations.0003_rekey_table_knowledge_to_logical_name"
)
rekey_forward = _migration.rekey_forward


@pytest.mark.django_db
def test_qualified_row_rekeyed_to_logical_name(workspace):
    TableKnowledge.objects.create(
        workspace=workspace,
        table_name="commcare_xyz_r1a2b3c4.cases",
        description="Case records",
        owner="Data Team",
    )

    rekey_forward(django_apps, None)

    assert not TableKnowledge.objects.filter(table_name__contains=".").exists()
    tk = TableKnowledge.objects.get(workspace=workspace, table_name="cases")
    assert tk.description == "Case records"
    assert tk.owner == "Data Team"


@pytest.mark.django_db
def test_collision_merges_into_existing_logical_row(workspace):
    # Auto-generated column-notes row keyed by the logical name.
    TableKnowledge.objects.create(
        workspace=workspace,
        table_name="cases",
        description="Auto-generated column notes for cases.",
        column_notes={"status": "Values: open, closed"},
    )
    # Human-curated qualified row.
    TableKnowledge.objects.create(
        workspace=workspace,
        table_name="commcare_xyz_r1a2b3c4.cases",
        description="",
        owner="Data Team",
        related_tables=[{"table": "forms", "join_hint": "cases.id = forms.case_id"}],
    )

    rekey_forward(django_apps, None)

    # Exactly one row remains, keyed by the logical name.
    rows = TableKnowledge.objects.filter(workspace=workspace, table_name="cases")
    assert rows.count() == 1
    tk = rows.get()
    # Curated values fill gaps; existing column_notes preserved.
    assert tk.owner == "Data Team"
    assert tk.related_tables == [{"table": "forms", "join_hint": "cases.id = forms.case_id"}]
    assert tk.column_notes == {"status": "Values: open, closed"}
    # No qualified rows left.
    assert not TableKnowledge.objects.filter(table_name__contains=".").exists()


@pytest.mark.django_db
def test_bare_rows_untouched(workspace):
    TableKnowledge.objects.create(
        workspace=workspace,
        table_name="forms",
        description="Form submissions",
    )
    rekey_forward(django_apps, None)
    tk = TableKnowledge.objects.get(workspace=workspace, table_name="forms")
    assert tk.description == "Form submissions"
