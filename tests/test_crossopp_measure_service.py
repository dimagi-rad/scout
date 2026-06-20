import pytest

from apps.transformations.models import CrossOppMeasure
from apps.transformations.services.measure_resolver import CanonicalMeasureSpec


@pytest.mark.django_db
def test_crossopp_measure_persists_and_roundtrips_spec(workspace):
    m = CrossOppMeasure.objects.create(
        workspace=workspace,
        name="birth_weight",
        description="newborn weight in grams",
        kind="numeric",
    )
    spec = m.to_spec()
    assert isinstance(spec, CanonicalMeasureSpec)
    assert (spec.name, spec.description, spec.kind) == (
        "birth_weight",
        "newborn weight in grams",
        "numeric",
    )


@pytest.mark.django_db
def test_measure_draft_holds_resolutions_and_flagged(workspace, user):
    from apps.transformations.models import CrossOppMeasureDraft

    d = CrossOppMeasureDraft.objects.create(
        workspace=workspace,
        name="length_of_stay",
        description="days in care",
        kind="numeric",
        thread_id="t-1",
        created_by=user,
        resolutions={
            "10012": {
                "column": "los_days",
                "status": "resolved",
                "confidence": 0.9,
            }
        },
        flagged=["10013"],
        shortlists={
            "10013": [{"column": "stay_len", "label": "Length of stay (days)", "type": "Int"}]
        },
        status="pending",
    )
    assert d.flagged == ["10013"]
    assert d.shortlists["10013"][0]["column"] == "stay_len"


def test_classify_doubt_flags_low_confidence_and_absent():
    from apps.transformations.services.crossopp_measure_service import classify_doubt
    from apps.transformations.services.measure_resolver import MeasureResolution

    def R(status, conf):
        return MeasureResolution("m", "c", "p", "c=1", conf, status, "lbl", "why")

    res = {
        "a": R("resolved", 0.9),
        "b": R("low_confidence", 0.3),
        "c": R("absent", 0.0),
    }
    has_doubt, flagged = classify_doubt(res)
    assert has_doubt is True
    assert sorted(flagged) == ["b", "c"]
    assert classify_doubt({"a": R("resolved", 0.9)}) == (False, [])


@pytest.mark.django_db
def test_add_measure_is_additive_and_preserves_existing(workspace, tmp_path):
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.crossopp_cube_builder import OppRef
    from apps.transformations.services.measure_resolver import (
        CanonicalMeasureSpec,
        MeasureResolution,
    )
    opps = [OppRef("10012", "t_10012_x"), OppRef("10013", "t_10013_y")]
    def R(col): return MeasureResolution("m", col, "p", col, 0.9, "resolved", "lbl", "why")
    # First measure
    svc.add_measure(workspace, CanonicalMeasureSpec("birth_weight", "g", "numeric"),
                    {"10012": R("child_weight_birth"), "10013": R("birth_weight")},
                    opps, model_root=str(tmp_path))
    model_after_first = (tmp_path / svc._ws_hash(workspace) / "canonical.yml").read_text()
    # Second measure
    svc.add_measure(workspace, CanonicalMeasureSpec("kmc_hours", "hrs", "numeric"),
                    {"10012": R("kmc_hours"), "10013": R("kmc_hours")},
                    opps, model_root=str(tmp_path))
    model_after_second = (tmp_path / svc._ws_hash(workspace) / "canonical.yml").read_text()
    # birth_weight's per-opp SELECT terms are still present, unchanged (stability)
    # _safe_numeric wraps the column in a CASE WHEN cast; check the actual rendered pattern
    assert "child_weight_birth)::numeric ELSE NULL END AS birth_weight" in model_after_first
    assert "child_weight_birth)::numeric ELSE NULL END AS birth_weight" in model_after_second
    assert "AS kmc_hours" in model_after_second
    # Both measures present in the blended cube
    from apps.transformations.models import CrossOppMeasure, CrossOppMeasureLineage
    names = set(
        CrossOppMeasure.objects.filter(workspace=workspace).values_list("name", flat=True)
    )
    assert names == {"birth_weight", "kmc_hours"}
    assert CrossOppMeasureLineage.objects.filter(workspace=workspace, measure="kmc_hours").count() == 2


@pytest.mark.django_db
def test_resolve_across_opps_uses_resolver_per_opp():
    import asyncio  # noqa: I001
    from apps.transformations.services import crossopp_measure_service as svc
    from apps.transformations.services.measure_resolver import CanonicalMeasureSpec
    spec = CanonicalMeasureSpec("mortality", "child died", "rate")
    cands = {"10012": [], "10013": []}
    class FakeClient:  # resolve_measure(model_client=...) path returns absent for [] candidates
        pass
    # With empty candidates resolve_measure returns 'absent' without an LLM call.
    res = asyncio.run(svc.resolve_across_opps_from_candidates(spec, cands))
    assert set(res) == {"10012", "10013"}
    assert all(r.status == "absent" for r in res.values())
