"""Tests for Connect staging SQL generator (Milestone 4 / Task 3)."""

from __future__ import annotations

import pytest

from apps.transformations.services.connect_staging import generate_connect_assets
from apps.users.models import Tenant

FORM_DEFS = {
    "muac_visit": {
        "name": "MUAC Visit",
        "deliver_unit": "muac_visit",
        "questions": [
            {
                "label": "MUAC (cm)",
                "value": "/data/muac_group/muac",
                "type": "Decimal",
                "repeat": False,
                "options": None,
            },
            {
                "label": "Confirmed",
                "value": "/data/muac_group/muac_confirmed",
                "type": "Select",
                "repeat": False,
                "options": ["yes", "no"],
            },
            {
                "label": "Child name",
                "value": "/data/children/child_name",
                "type": "Text",
                "repeat": True,
                "options": None,
            },
        ],
    }
}


@pytest.fixture
def connect_tenant(db):
    """Create a Tenant with provider commcare_connect."""
    return Tenant.objects.create(
        provider="commcare_connect",
        external_id="1237",
        canonical_name="Connect Opp 1237",
    )


@pytest.mark.django_db(transaction=True)
def test_generates_visit_staging_with_typed_columns(connect_tenant):
    assets = generate_connect_assets(FORM_DEFS, connect_tenant)
    by_name = {a.name: a for a in assets}

    assert "stg_visits" in by_name
    sql = by_name["stg_visits"].sql_content
    # Non-repeat questions become typed, aliased columns from form_json:
    assert "form_json" in sql
    assert "muac" in sql  # column derived from /data/muac_group/muac
    assert "muac_confirmed" in sql
    # Repeat question is NOT inlined into stg_visits:
    assert "child_name" not in sql

    # Repeat group becomes its own child asset:
    assert "stg_visits__repeat_children" in by_name
    rsql = by_name["stg_visits__repeat_children"].sql_content
    assert "jsonb_array_elements" in rsql
    assert "child_name" in rsql
