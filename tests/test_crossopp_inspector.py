"""Tests for the cross-opp transparency inspector payload."""

from __future__ import annotations

import pytest

from apps.transformations.models import CrossOppMeasureLineage
from apps.users.models import User
from apps.workspaces.api.crossopp_views import inspector_payload
from apps.workspaces.models import Workspace


@pytest.mark.django_db
def test_inspector_payload_groups_lineage_with_coverage_and_provenance():
    user = User.objects.create(email="inspector@example.com")
    ws = Workspace.objects.create(name="KMC Cross-Opp Test", created_by=user)

    CrossOppMeasureLineage.objects.create(
        workspace=ws,
        opportunity_id="10012",
        measure="birth_weight",
        column="child_weight_birth",
        source_path="/data/child_details/birth_weight_group/child_weight_birth",
        matched_label="Stable SVN weight at the time of birth(in grams)",
        sql_expression="CAST(child_weight_birth AS NUMERIC)",
        confidence=0.97,
        status="resolved",
    )
    CrossOppMeasureLineage.objects.create(
        workspace=ws,
        opportunity_id="10020",
        measure="birth_weight",
        status="absent",
    )

    payload = inspector_payload(ws)
    assert payload["workspace_id"] == str(ws.id)
    assert payload["schema_name"].startswith("ws_")

    bw = next(m for m in payload["measures"] if m["measure"] == "birth_weight")
    assert bw["coverage"] == {"resolved": 1, "low_confidence": 0, "absent": 1, "total": 2}

    resolved = next(o for o in bw["opps"] if o["opportunity_id"] == "10012")
    # the full provenance the inspector shows so a user can verify the number
    assert resolved["source_path"].endswith("child_weight_birth")
    assert "weight" in resolved["matched_label"].lower()
    assert resolved["sql_expression"] == "CAST(child_weight_birth AS NUMERIC)"
    assert resolved["confidence"] == 0.97

    absent = next(o for o in bw["opps"] if o["opportunity_id"] == "10020")
    assert absent["status"] == "absent"
    assert absent["column"] == ""


def test_dashboard_query_sql_built_from_model():
    from apps.workspaces.api.crossopp_views import dashboard_query_sql

    model = (
        "cubes:\n"
        "- name: opp_10012\n"
        "  sql: SELECT 1\n"  # per-opp cube: no measures, skipped
        "- name: kmc_cross_opp\n"
        "  sql: SELECT 1\n"
        "  dimensions:\n"
        "  - {name: opportunity_id, sql: opportunity_id, type: string}\n"
        "  measures:\n"
        "  - {name: visits, type: count}\n"
        "  - {name: birth_weight, sql: birth_weight, type: avg}\n"
    )
    sql = dashboard_query_sql(model)
    assert "FROM kmc_cross_opp" in sql
    assert "MEASURE(visits)" in sql
    assert "MEASURE(birth_weight)" in sql
    assert "GROUP BY opportunity_id ORDER BY opportunity_id" in sql


def test_dashboard_query_sql_none_without_blended_cube():
    from apps.workspaces.api.crossopp_views import dashboard_query_sql

    assert dashboard_query_sql("cubes:\n- name: opp_1\n  sql: SELECT 1\n") is None
