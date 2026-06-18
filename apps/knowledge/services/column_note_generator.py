"""
Auto-populate TableKnowledge.column_notes from Connect form_definitions.

Iterates non-repeat questions across form_definitions (shape produced by
_extract_form_definitions in apps/transformations/services/commcare_staging.py):

    {<xmlns>: {"questions": [{"label": str, "value": str, "type": str, "repeat": bool,
                               "options"?: list | None, "choices"?: list | None}, ...]}, ...}

Derives column names using the same _column_name_from_path helper as Task 3
(staging column construction) so names stay aligned.
"""

from apps.knowledge.models import TableKnowledge
from apps.transformations.services.commcare_staging import _column_name_from_path


async def sync_column_notes(workspace, table_name: str, form_definitions: dict) -> TableKnowledge:
    """
    Upsert TableKnowledge for *table_name*, merging per-column notes derived
    from question label + type + choices/options across all forms in
    *form_definitions*.

    Column names are derived via _column_name_from_path to match the staging
    column names produced by Task 3 exactly.

    Returns the upserted TableKnowledge instance.
    """
    column_notes: dict[str, str] = {}

    for _xmlns, form_def in form_definitions.items():
        for question in form_def.get("questions", []):
            # Skip repeat groups — they stage to separate tables
            if question.get("repeat"):
                continue

            value_path = question.get("value", "")
            if not value_path:
                continue

            col_name = _column_name_from_path(value_path)
            label = question.get("label", col_name)
            qtype = question.get("type", "")

            # Build note string: "label — type" (optionally "; values: a, b, c")
            note = f"{label} — {qtype}"

            # Accept either "options" or "choices" key (tolerate missing / None)
            raw_choices = question.get("options") or question.get("choices")
            if raw_choices:
                values_str = ", ".join(str(c) for c in raw_choices)
                note = f"{note}; values: {values_str}"

            column_notes[col_name] = note

    tk, _created = await TableKnowledge.objects.aupdate_or_create(
        workspace=workspace,
        table_name=table_name,
        defaults={
            "column_notes": column_notes,
            "description": f"Auto-generated column notes for {table_name}.",
        },
    )
    return tk
