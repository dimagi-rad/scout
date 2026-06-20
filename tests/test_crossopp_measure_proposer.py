import pytest


@pytest.mark.asyncio
async def test_proposer_emits_specs_from_candidates():
    from apps.transformations.services import crossopp_measure_proposer as p
    from apps.transformations.services.crossopp_measure_proposer import _Proposed, _ProposedList
    from apps.transformations.services.measure_resolver import FieldCandidate

    fc = FieldCandidate(
        "/d/w", "w", "child_weight_birth", "Birth weight (g)", "Double", "Reg", "Visit", "child", True
    )

    class FakeLLM:
        async def ainvoke(self, messages):
            return _ProposedList(
                measures=[_Proposed(name="birth_weight", description="g", kind="numeric")]
            )

    specs = await p.propose_measures({"10012": [fc]}, model_client=FakeLLM(), limit=5)
    assert specs[0].name == "birth_weight" and specs[0].kind == "numeric"
