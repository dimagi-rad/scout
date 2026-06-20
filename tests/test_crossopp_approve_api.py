"""Tests for POST /api/workspaces/<id>/crossopp/measures/<draft_id>/approve/."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.workspaces.models import Workspace

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_resume_after_approval_reinvokes_agent(monkeypatch):
    from unittest.mock import patch

    from apps.workspaces import tasks

    user = await User.objects.acreate_user(email="resume_approval@b.c", password="x")
    ws = await Workspace.objects.acreate(name="WApproval", created_by=user)

    seen = {}

    class FakeAgent:
        async def ainvoke(self, state, config):
            seen["state"] = state
            seen["config"] = config

    async def fake_build(workspace, u, conversation_id=None):
        return FakeAgent(), {}

    with patch.object(tasks, "_build_agent_for_resume", fake_build):
        out = await tasks.resume_thread_after_measure_approval(
            None,
            workspace_id=str(ws.id),
            thread_id="thread-1",
            measure_name="los",
        )

    assert out["status"] == "resumed"
    assert "los" in seen["state"]["messages"][0].content
    assert seen["config"]["configurable"]["thread_id"] == "thread-1"


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


@pytest.mark.django_db(transaction=True)
def test_approve_reject_marks_opp_absent(workspace, user, monkeypatch):
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
        {"overrides": {"10013": {"action": "reject"}}},
        format="json",
    )

    assert resp.status_code == 200
    assert captured["res"]["10013"].status == "absent"
    assert captured["res"]["10013"].sql_expression is None


@pytest.mark.django_db(transaction=True)
def test_approve_ignores_unknown_opp_id(workspace, user, monkeypatch):
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
        {"overrides": {"99999": {"action": "reject"}}},
        format="json",
    )

    assert resp.status_code == 200
    assert "10012" in captured["res"]
    assert "10013" in captured["res"]
    assert captured["res"]["10012"].status == "resolved"
    assert captured["res"]["10013"].status == "low_confidence"
