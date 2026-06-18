import pytest

from apps.knowledge.models import TableKnowledge
from apps.knowledge.services.column_note_generator import sync_column_notes

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
