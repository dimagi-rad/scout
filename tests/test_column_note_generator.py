import pytest

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
