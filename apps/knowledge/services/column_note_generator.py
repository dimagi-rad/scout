"""
Auto-populate TableKnowledge.column_notes from Connect form_definitions.

Iterates non-repeat questions across form_definitions (shape produced by
_extract_form_definitions in apps/transformations/services/commcare_staging.py):

    {<xmlns>: {"questions": [{"label": str, "value": str, "type": str, "repeat": bool,
                               "options"?: list | None, "choices"?: list | None}, ...]}, ...}

Derives column names using visit_column_map from connect_staging so that
the final deduped column names always match the staging columns produced by
_generate_stg_visits — including collision-suffixed names like ``status_2``.
"""

from asgiref.sync import sync_to_async
from django.db import transaction

from apps.knowledge.models import TableKnowledge
from apps.transformations.services.connect_staging import (
    folded_visit_aliases,
    visit_column_map,
)


async def sync_column_notes(workspace, table_name: str, form_definitions: dict) -> TableKnowledge:
    """
    Upsert TableKnowledge for *table_name*, merging per-column notes derived
    from question label + type + choices/options across all forms in
    *form_definitions*.

    Column names are derived via visit_column_map to match the staging column
    names produced by _generate_stg_visits exactly, including any collision-
    suffixed names (e.g. ``status_2`` when ``status`` is a base column).

    Returns the upserted TableKnowledge instance.
    """
    column_notes: dict[str, str] = {}

    for question, col_name in visit_column_map(form_definitions):
        label = question.get("label", col_name)
        qtype = question.get("type", "")

        note = f"{label} — {qtype}"

        # Forms use either "options" or "choices"; tolerate missing/None.
        raw_choices = question.get("options") or question.get("choices")
        if raw_choices:
            values_str = ", ".join(str(c) for c in raw_choices)
            note = f"{note}; values: {values_str}"

        column_notes[col_name] = note

    # A sync before #712 noted every field, including ones now folded into form_json;
    # the agent would be told about columns that do not exist.
    folded = folded_visit_aliases(form_definitions)
    return await _merge_column_notes(workspace, table_name, column_notes, folded)


@sync_to_async
def _merge_column_notes(workspace, table_name, column_notes, folded) -> TableKnowledge:
    # Connect sources sharing a workspace can load at once; without the row lock
    # one source's merge would overwrite the other's notes.
    TableKnowledge.objects.get_or_create(
        workspace=workspace,
        table_name=table_name,
        defaults={
            "column_notes": {},
            "description": f"Auto-generated column notes for {table_name}.",
        },
    )
    with transaction.atomic():
        tk = TableKnowledge.objects.select_for_update().get(
            workspace=workspace, table_name=table_name
        )
        kept_existing = {
            name: note for name, note in (tk.column_notes or {}).items() if name not in folded
        }
        tk.column_notes = {**kept_existing, **column_notes}
        tk.save(update_fields=["column_notes", "updated_at"])
    return tk
