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
