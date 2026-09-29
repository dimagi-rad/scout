import pytest

from apps.knowledge.models import TableKnowledge
from apps.knowledge.services.column_note_generator import sync_column_notes
from apps.transformations.services.commcare_staging import MAX_STAGING_COLUMNS

FORM_DEFS = {
    "muac_visit": {
        "questions": [
            {
                "label": "MUAC (cm)",
                "value": "/data/muac_group/muac",
                "type": "Decimal",
                "options": None,
                "repeat": False,
            },
            {
                "label": "Confirmed",
                "value": "/data/muac_group/muac_confirmed",
                "type": "Select",
                "options": ["yes", "no"],
                "repeat": False,
            },
        ]
    }
}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_column_notes_populates_from_form_defs(workspace):
    tk = await sync_column_notes(workspace, "stg_visits", FORM_DEFS)
    assert "Decimal" in tk.column_notes["muac"]
    assert "yes, no" in tk.column_notes["muac_confirmed"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_column_notes_merges_and_preserves_human_data(workspace):
    await sync_column_notes(workspace, "stg_visits", FORM_DEFS)
    tk = await TableKnowledge.objects.aget(workspace=workspace, table_name="stg_visits")
    tk.column_notes["supervisor_note"] = "Added by human"
    tk.description = "Human description"
    await tk.asave()

    tk2 = await sync_column_notes(workspace, "stg_visits", FORM_DEFS)
    assert tk2.column_notes.get("supervisor_note") == "Added by human"  # human note survives
    assert "muac" in tk2.column_notes  # auto note still present
    assert tk2.description == "Human description"  # human description NOT clobbered


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sync_column_notes_drops_notes_for_folded_fields(workspace):
    wide = {
        "visit": {
            "questions": [
                {"label": f"Q{index}", "value": f"/data/q{index:04d}", "type": "Text"}
                for index in range(2100)
            ]
        }
    }
    # Notes synced before #712 folding existed cover every field.
    await TableKnowledge.objects.acreate(
        workspace=workspace,
        table_name="stg_visits",
        column_notes={"q2099": "stale", "q0000": "old", "supervisor_note": "Added by human"},
    )

    tk = await sync_column_notes(workspace, "stg_visits", wide)

    assert "q2099" not in tk.column_notes
    assert tk.column_notes["q0000"] == "Q0 — Text"
    assert tk.column_notes["supervisor_note"] == "Added by human"
    assert len(tk.column_notes) == MAX_STAGING_COLUMNS - 7 + 1
