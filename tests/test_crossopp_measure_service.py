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
