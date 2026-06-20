"""Tests for POST /api/workspaces/<id>/crossopp/measures/<draft_id>/approve/."""

import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db(transaction=True)
def test_approve_applies_overrides_and_commits(workspace, user, monkeypatch):
    from apps.transformations.models import CrossOppMeasureDraft
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef

    monkeypatch.setattr(
        svc,
        "workspace_opps",
        lambda ws: ([OppRef("10012", "s"), OppRef("10013", "s2")], {}),
    )
    captured = {}

    def fake_add(ws, spec, resolutions, opps, **k):
        captured["res"] = resolutions
        return [{"opportunity_id": o, "status": resolutions[o].status} for o in resolutions]

    monkeypatch.setattr(svc, "add_measure", fake_add)
    monkeypatch.setattr(
        "apps.workspaces.api.crossopp_views._defer_measure_resume", lambda *a, **k: None
    )

    draft = CrossOppMeasureDraft.objects.create(
        workspace=workspace,
        name="los",
        description="d",
        kind="numeric",
        thread_id="t1",
        created_by=user,
        status="pending",
        resolutions={
            "10012": {
                "measure": "los",
                "column": "a",
                "source_path": "",
                "sql_expression": "a",
                "confidence": 0.9,
                "status": "resolved",
                "matched_label": "",
                "reason": "",
            },
            "10013": {
                "measure": "los",
                "column": None,
                "source_path": "",
                "sql_expression": None,
                "confidence": 0.2,
                "status": "low_confidence",
                "matched_label": "",
                "reason": "",
            },
        },
        flagged=["10013"],
        shortlists={"10013": [{"column": "stay_len", "label": "Stay", "type": "Int"}]},
    )

    c = APIClient()
    c.force_authenticate(user=user)
    resp = c.post(
        f"/api/workspaces/{workspace.id}/crossopp/measures/{draft.id}/approve/",
        {"overrides": {"10013": {"action": "pick", "column": "stay_len"}}},
        format="json",
    )

    assert resp.status_code == 200 and resp.data["status"] == "committed"
    assert captured["res"]["10013"].column == "stay_len"
    assert captured["res"]["10013"].status == "resolved"
    draft.refresh_from_db()
    assert draft.status == "committed"
