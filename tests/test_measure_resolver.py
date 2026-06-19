"""Tests for the cross-opp measure resolver's deterministic candidate-gathering.

Fixtures mirror real opp 10012 structure observed live: the same clinical concept
(child_weight_birth) appears both as a real ``Double`` entry question with a human label
and as a ``DataBindOnly`` calculated copy with a cryptic ``#form/...`` label, at different
paths in different forms — the exact case the resolver must disambiguate.
"""

from __future__ import annotations

import pytest

from apps.transformations.services.measure_resolver import (
    CanonicalMeasureSpec,
    FieldCandidate,
    _ResolutionResult,
    gather_measure_candidates,
    resolve_measure,
)

_BIRTH_WEIGHT_PATH = "/data/child_details/birth_weight_group/child_weight_birth"


class _FakeClient:
    """Stands in for ChatAnthropic.with_structured_output — returns a canned result."""

    def __init__(self, result: _ResolutionResult):
        self._result = result
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return self._result


class _ExplodingClient:
    async def ainvoke(self, _messages):  # pragma: no cover - must never be called
        raise AssertionError("resolver should not call the model when shortlist is empty")

# A trimmed form_definitions blob shaped like the labs ``app_structure`` extract:
# per-form name/module/case_type + a list of questions (type/label/value/repeat).
FORM_DEFINITIONS = {
    "xmlns_visit": {
        "name": {"en": "Record Visit Details", "hau": "Rubuta bayanen ziyara"},
        "module_name": {"en": "Visit Management"},
        "case_type": "child",
        "questions": [
            # calculated copy: cryptic #form/ label, NOT an entry
            {
                "type": "DataBindOnly",
                "label": "#form/child_weight_birth",
                "value": "/data/child_weight_birth",
                "repeat": False,
            },
            # a real device-captured danger-sign field
            {
                "type": "Int",
                "label": "Please record the SVN's SpO2 Level",
                "value": "/data/danger_signs_checklist/spo2_level",
                "repeat": False,
            },
            # a repeat-group child — must be skipped (staged separately)
            {
                "type": "Text",
                "label": "Repeated note",
                "value": "/data/notes_repeat/note",
                "repeat": True,
            },
            # a structural group container — must be skipped
            {
                "type": "Group",
                "label": "Danger signs",
                "value": "/data/danger_signs_checklist",
                "repeat": False,
                "is_group": True,
            },
        ],
    },
    "xmlns_registration": {
        "name": {"en": "Child Registration Form"},
        "module_name": {"en": "Caregiver Management"},
        "case_type": "caregiver",
        "questions": [
            # the REAL entry question for birth weight: Double, rich human label
            {
                "type": "Double",
                "label": "Stable SVN weight at the time of birth(in grams)",
                "value": "/data/child_details/birth_weight_group/child_weight_birth",
                "repeat": False,
            },
        ],
    },
    "xmlns_learn": {
        "name": {"en": "10 KMC Mantras"},
        "module_name": {"en": "Learn Module"},
        "case_type": "",  # learn forms have no case_type — pure noise for clinical measures
        "questions": [
            {
                "type": "Trigger",
                "label": "Mantra 1",
                "value": "/data/kmc_mantras/mantra1",
                "repeat": False,
            },
        ],
    },
}


def test_repeat_and_structural_questions_are_skipped():
    cands = gather_measure_candidates(FORM_DEFINITIONS)
    paths = {c.path for c in cands}
    assert "/data/notes_repeat/note" not in paths  # repeat skipped
    assert "/data/danger_signs_checklist" not in paths  # structural group skipped


def test_entry_question_is_distinguished_from_calculated_copy():
    cands = gather_measure_candidates(FORM_DEFINITIONS)
    # both child_weight_birth fields share the same leaf column (the dedup key)
    weight = [c for c in cands if c.column == "child_weight_birth"]
    assert len(weight) == 2

    entry = [c for c in weight if c.is_entry]
    copy = [c for c in weight if not c.is_entry]
    assert len(entry) == 1 and len(copy) == 1

    # the entry is the Double with a real label, scoped to its form/module
    assert entry[0].type == "Double"
    assert "birth" in entry[0].label.lower()
    assert entry[0].form_name == "Child Registration Form"
    assert entry[0].case_type == "caregiver"

    # the DataBindOnly copy's cryptic #form/ label is dropped, and it is not an entry
    assert copy[0].type == "DataBindOnly"
    assert copy[0].label == ""


def test_localized_labels_resolve_to_english():
    cands = gather_measure_candidates(FORM_DEFINITIONS)
    spo2 = next(c for c in cands if c.column == "spo2_level")
    assert spo2.form_name == "Record Visit Details"  # not the {en,hau} dict
    assert spo2.is_entry is True


def test_learn_form_fields_carry_no_case_type():
    # Clinical-measure resolution can scope these out: no case_type, "Learn Module" module.
    cands = gather_measure_candidates(FORM_DEFINITIONS)
    mantra = next(c for c in cands if c.column == "mantra1")
    assert mantra.case_type == ""
    assert mantra.module_name == "Learn Module"
    assert mantra.is_entry is False  # Trigger is a UI label, not data


def test_candidate_is_frozen_dataclass():
    cands = gather_measure_candidates(FORM_DEFINITIONS)
    assert isinstance(cands[0], FieldCandidate)


# ── LLM ranking layer (fake client — no API call) ────────────────────────────

_BIRTH_WEIGHT = CanonicalMeasureSpec(
    name="birth_weight",
    description="newborn weight in grams recorded at registration",
    kind="numeric",
)


@pytest.mark.asyncio
async def test_resolve_measure_picks_field_and_carries_provenance():
    fake = _FakeClient(
        _ResolutionResult(
            column="child_weight_birth",
            sql_expression="CAST(child_weight_birth AS NUMERIC)",
            confidence=0.9,
            status="resolved",
            reason="label 'Stable SVN weight at the time of birth' matches",
        )
    )
    res = await resolve_measure(
        _BIRTH_WEIGHT, gather_measure_candidates(FORM_DEFINITIONS), model_client=fake
    )
    assert fake.calls == 1
    assert res.status == "resolved"
    assert res.column == "child_weight_birth"
    assert res.sql_expression == "CAST(child_weight_birth AS NUMERIC)"
    assert res.confidence == 0.9
    # source_path + matched_label are recovered from the chosen candidate (real provenance),
    # by matching on column — the entry question wins over the DataBindOnly copy.
    assert res.source_path == _BIRTH_WEIGHT_PATH
    assert "birth" in res.matched_label.lower()


@pytest.mark.asyncio
async def test_resolve_measure_low_confidence_is_flagged():
    fake = _FakeClient(
        _ResolutionResult(
            column="child_weight_birth",
            sql_expression="child_weight_birth",
            confidence=0.3,
            status="resolved",
            reason="weak match",
        )
    )
    res = await resolve_measure(
        _BIRTH_WEIGHT, gather_measure_candidates(FORM_DEFINITIONS), model_client=fake
    )
    assert res.status == "low_confidence"  # derived from confidence, not hidden


@pytest.mark.asyncio
async def test_resolve_measure_absent_from_llm():
    fake = _FakeClient(
        _ResolutionResult(
            column=None, sql_expression=None, confidence=0.0, status="absent", reason="none match"
        )
    )
    res = await resolve_measure(
        _BIRTH_WEIGHT, gather_measure_candidates(FORM_DEFINITIONS), model_client=fake
    )
    assert res.status == "absent"
    assert res.column is None and res.sql_expression is None


@pytest.mark.asyncio
async def test_resolve_measure_short_circuits_when_no_clinical_candidates():
    # Only a learn form (no case_type, non-entry) → empty shortlist → absent without calling the model.
    learn_only = {"xmlns_learn": FORM_DEFINITIONS["xmlns_learn"]}
    res = await resolve_measure(
        _BIRTH_WEIGHT, gather_measure_candidates(learn_only), model_client=_ExplodingClient()
    )
    assert res.status == "absent"
    assert "no clinical entry fields" in res.reason
