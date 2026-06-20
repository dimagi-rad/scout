import pytest


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_measure_commits_when_no_doubt(workspace, user, monkeypatch):
    from apps.agents.tools import crossopp_measure_tool as t
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    from apps.transformations.services.measure_resolver import MeasureResolution

    def R():
        return MeasureResolution("m", "c", "p", "c", 0.9, "resolved", "lbl", "why")

    monkeypatch.setattr(svc, "workspace_opps", lambda ws: ([OppRef("10012", "s")], {"10012": []}))

    async def fake_resolve(spec, cands, **k):
        return {"10012": R()}

    monkeypatch.setattr(svc, "resolve_across_opps_from_candidates", fake_resolve)

    async def fake_aadd_measure(*a, **k):
        return [{"opportunity_id": "10012", "status": "resolved"}]

    monkeypatch.setattr(svc, "aadd_measure", fake_aadd_measure)

    async def ok(*a, **k):
        return True

    monkeypatch.setattr(svc, "ensure_measure_queryable_meta", ok)

    [define] = t.create_crossopp_measure_tools(workspace, user, "thread-1")
    out = await define.ainvoke({"name": "birth_weight", "description": "g", "kind": "numeric"})
    assert out["status"] == "committed" and out["measure"] == "birth_weight"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_measure_drafts_when_doubt(workspace, user, monkeypatch):
    from apps.agents.tools import crossopp_measure_tool as t
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    from apps.transformations.services.measure_resolver import MeasureResolution

    monkeypatch.setattr(
        svc,
        "workspace_opps",
        lambda ws: (
            [OppRef("10012", "s"), OppRef("10013", "s2")],
            {"10012": [], "10013": []},
        ),
    )

    async def fake_resolve(spec, cands, **k):
        return {
            "10012": MeasureResolution("m", "c", "p", "c", 0.9, "resolved", "lbl", "y"),
            "10013": MeasureResolution("m", None, None, None, 0.2, "low_confidence", "", "y"),
        }

    monkeypatch.setattr(svc, "resolve_across_opps_from_candidates", fake_resolve)
    monkeypatch.setattr(svc, "shortlist_for_opp", lambda c: [{"column": "x", "label": "X", "type": "Int"}])

    [define] = t.create_crossopp_measure_tools(workspace, user, "thread-1")
    out = await define.ainvoke({"name": "los", "description": "days", "kind": "numeric"})
    assert out["status"] == "needs_approval"
    assert out["flagged"][0]["opp_id"] == "10013"
    from apps.transformations.models import CrossOppMeasureDraft

    assert await CrossOppMeasureDraft.objects.filter(id=out["draft_id"]).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_tool_registered_in_agent(workspace, user):
    from apps.agents.graph.base import _build_tools

    tools = _build_tools(workspace, user, mcp_tools=[], conversation_id="t-1")
    assert any(getattr(t, "name", "") == "define_crossopp_measure" for t in tools)
