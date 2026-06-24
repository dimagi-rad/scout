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

from apps.knowledge.models import TableKnowledge
from apps.transformations.services.connect_staging import visit_column_map


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

        # Build note string: "label — type" (optionally "; values: a, b, c")
        note = f"{label} — {qtype}"

        # Accept either "options" or "choices" key (tolerate missing / None)
        raw_choices = question.get("options") or question.get("choices")
        if raw_choices:
            values_str = ", ".join(str(c) for c in raw_choices)
            note = f"{note}; values: {values_str}"

        column_notes[col_name] = note

    existing = await TableKnowledge.objects.filter(
        workspace=workspace, table_name=table_name
    ).afirst()
    merged_notes = {**(existing.column_notes if existing else {}), **column_notes}

    tk, _created = await TableKnowledge.objects.aupdate_or_create(
        workspace=workspace,
        table_name=table_name,
        defaults={"column_notes": merged_notes},
        create_defaults={
            "column_notes": merged_notes,
            "description": f"Auto-generated column notes for {table_name}.",
        },
    )
    return tk
