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

    define, _propose, _vf, _redef = t.create_crossopp_measure_tools(workspace, user, "thread-1")
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

    define, _propose, _vf, _redef = t.create_crossopp_measure_tools(workspace, user, "thread-1")
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


def test_propose_tool_in_factory(workspace, user):
    from apps.agents.tools import crossopp_measure_tool as t

    tools = t.create_crossopp_measure_tools(workspace, user, "thread-1")
    names = [getattr(tool, "name", "") for tool in tools]
    assert "propose_crossopp_measures" in names


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_define_visit_field_commits_via_add_visit_field(workspace, user, monkeypatch):
    """The chat-driven per-visit modeling: defining a per-visit canonical field (e.g.
    visit_weight) resolves it across opps and commits via add_visit_field, so the cube's
    growth surface materializes from cold."""
    from apps.agents.tools import crossopp_measure_tool as t
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    from apps.transformations.services.measure_resolver import MeasureResolution

    monkeypatch.setattr(svc, "workspace_opps", lambda ws: ([OppRef("10012", "s")], {"10012": []}))

    async def fake_resolve(spec, cands, **k):
        return {
            "10012": MeasureResolution(
                "visit_weight", "child_weight_visit", "p", "child_weight_visit",
                0.95, "resolved", "lbl", "y",
            )
        }

    monkeypatch.setattr(svc, "resolve_across_opps_from_candidates", fake_resolve)
    captured = {}

    def fake_add_visit_field(ws, name, res, opps, **k):
        captured["name"] = name

    monkeypatch.setattr(svc, "add_visit_field", fake_add_visit_field)

    async def ok(*a, **k):
        return True

    monkeypatch.setattr(svc, "ensure_measure_queryable_meta", ok)

    tools = t.create_crossopp_measure_tools(workspace, user, "thread-1")
    vf = next(x for x in tools if getattr(x, "name", "") == "define_crossopp_visit_field")
    out = await vf.ainvoke({"name": "visit_weight", "description": "infant weight at the visit"})
    assert out["status"] == "committed"
    assert out["field"] == "visit_weight"
    assert captured["name"] == "visit_weight"
    assert out["lineage"][0]["opportunity_id"] == "10012"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_visit_field_tool_registered_in_agent(workspace, user):
    from apps.agents.graph.base import _build_tools

    tools = _build_tools(workspace, user, mcp_tools=[], conversation_id="t-1")
    assert any(getattr(t, "name", "") == "define_crossopp_visit_field" for t in tools)
