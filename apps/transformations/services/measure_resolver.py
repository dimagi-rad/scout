"""Resolve canonical measures to per-opp SQL expressions (the cross-opp auto-model).

A canonical measure is a domain concept named in plain language (e.g. "birth_weight —
newborn weight in grams recorded at registration"). For a given opportunity (tenant), the
resolver picks the field in *that* app's ``stg_visits`` that best represents the concept and
emits a SQL expression aliased to the canonical column name. This is the one piece Cube
cannot do — Cube runs the SQL we give it; it does not reconcile differently-named fields.

This module provides the deterministic substrate the resolver ranks over:
``gather_measure_candidates`` flattens a tenant's ``form_definitions`` into typed, labeled,
form-scoped field candidates, distinguishing real data-entry questions from calculated
copies (``DataBindOnly``) and UI labels (``Trigger``). The LLM ranking layer (added next)
selects among these; keeping candidate-gathering pure makes it testable without an API call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from apps.transformations.services.commcare_staging import _column_name_from_path
from apps.transformations.services.connect_staging import (
    _is_structural_question,
    _to_form_json_path,
)

# Question ``type`` values that represent a real, user-entered (or device-captured) answer
# — the source of truth for a concept. Everything else (``DataBindOnly`` calculated copies,
# ``Trigger`` UI labels) is NOT an entry: it may carry the same leaf name but a cryptic
# ``#form/...`` label and no independent data.
ENTRY_TYPES = frozenset(
    {
        "Text",
        "Integer",
        "Int",
        "Decimal",
        "Double",
        "Select",
        "MSelect",
        "Date",
        "DateTime",
        "Time",
        "Geopoint",
        "Image",
        "Barcode",
        "PhoneNumber",
    }
)


@dataclass(frozen=True)
class FieldCandidate:
    """One ``stg_visits`` field offered to the resolver as a possible match for a measure."""

    path: str  # deliver-app question xpath, e.g. /data/child_details/.../child_weight_birth
    json_path: str  # the form_json extraction path used in staging SQL
    column: str  # leaf-derived column name (the dedup key shared across forms)
    label: str  # human label (the primary semantic signal); "" for cryptic #form/... copies
    type: str  # CommCare question type
    form_name: str  # e.g. "Record Visit Details"
    module_name: str  # e.g. "Visit Management"
    case_type: str  # e.g. "child"
    is_entry: bool  # True = real data-entry question; False = DataBindOnly/Trigger copy
    samples: tuple[str, ...] = ()  # a few real values from stg_visits — lets the resolver
    # avoid placeholder/garbage columns (e.g. child_age_2 = "sample-101") it can't spot by label


def _localized(value) -> str:
    """Forms/modules carry multilingual labels (``{"en": ..., "hau": ...}``); take English."""
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), "")
    return value or ""


def _clean_label(label: str) -> str:
    """Drop the cryptic ``#form/...`` auto-labels CommCare gives calculated copies.

    A ``DataBindOnly`` question's label is typically ``#form/<path>`` — not a human
    description, and actively misleading as a matching signal. Treat it as no label.
    """
    if not label or label.startswith("#"):
        return ""
    return label


def gather_measure_candidates(form_definitions: dict) -> list[FieldCandidate]:
    """Flatten ``form_definitions`` into ranked-ready field candidates.

    Mirrors ``connect_staging`` filters (skip repeat-group and structural questions) so the
    candidate set matches what actually becomes a ``stg_visits`` column. Each candidate
    carries its form/module/case_type scope and an ``is_entry`` flag so the resolver can
    prefer the real entry question over a ``DataBindOnly`` copy that shares its leaf name.
    """
    candidates: list[FieldCandidate] = []
    for form in form_definitions.values():
        form_name = _localized(form.get("name"))
        module_name = _localized(form.get("module_name"))
        case_type = form.get("case_type", "") or ""
        for q in form.get("questions", []):
            if q.get("repeat") or _is_structural_question(q):
                continue
            value_path = q.get("value", "")
            if not value_path:
                continue
            q_type = q.get("type", "") or ""
            candidates.append(
                FieldCandidate(
                    path=value_path,
                    json_path=_to_form_json_path(value_path),
                    column=_column_name_from_path(value_path),
                    label=_clean_label(q.get("label", "")),
                    type=q_type,
                    form_name=form_name,
                    module_name=module_name,
                    case_type=case_type,
                    is_entry=q_type in ENTRY_TYPES,
                )
            )
    return candidates


# ── LLM ranking layer ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CanonicalMeasureSpec:
    """A domain concept the expert wants to measure — named in plain language, not fields.

    ``kind`` shapes the expected SQL: ``numeric`` → the field's value (for avg/sum);
    ``rate`` → a boolean predicate (for an averaged 0/1 rate, e.g. danger-sign rate).
    """

    name: str  # canonical column/slug, e.g. "birth_weight"
    description: str  # what it means, in domain terms
    kind: Literal["numeric", "rate"] = "numeric"


@dataclass(frozen=True)
class MeasureResolution:
    """How one canonical measure resolves for one opp (the auto-model's per-opp output)."""

    measure: str
    column: str | None  # the stg_visits column the expression reads (None when absent)
    source_path: str | None  # the deliver-app question xpath that column came from (provenance)
    sql_expression: str | None  # SQL over stg_visits aliased to the canonical name (None when absent)
    confidence: float
    status: Literal["resolved", "low_confidence", "absent"]
    matched_label: str  # the candidate label the LLM matched on (provenance)
    reason: str


class _ResolutionResult(BaseModel):
    """Structured LLM output (Simon #303: structured output, not hand-parsed text)."""

    column: str | None = Field(
        default=None,
        description="The snake_case column (the `column=` value) your sql_expression reads, or null if absent.",
    )
    sql_expression: str | None = Field(
        default=None,
        description="SQL over stg_visits yielding the measure (a value for numeric, a boolean for rate), or null.",
    )
    confidence: float = Field(description="0.0-1.0 confidence in this mapping.")
    status: Literal["resolved", "absent"] = Field(
        description="resolved if a field represents the measure; absent if none does."
    )
    reason: str = Field(description="One sentence: why this field (or why absent).")


_LOW_CONFIDENCE = 0.5
_MAX_SHORTLIST = 250

_SYSTEM_PROMPT = (
    "You map ONE clinical program measure to a single field in a CommCare app's visit data. "
    "You are given the measure and a shortlist of candidate fields, each with a human label, "
    "type, the form/module it lives in, and its case type. Rules:\n"
    "- Match on the field's LABEL meaning, not its name.\n"
    "- Pick exactly ONE field that best represents the measure, or status=absent if none does.\n"
    "- For a 'numeric' measure, sql_expression is the field value (cast to numeric if needed).\n"
    "- For a 'rate' measure, sql_expression is a BOOLEAN predicate that is true when the "
    "event occurred (it will be averaged into a 0..1 rate).\n"
    "- Write sql_expression over the staged columns by their snake_case column name "
    "(given as `column=`), e.g. child_weight_birth, or (danger_sign_positive = 'yes').\n"
    "- Prefer fields from the clinical visit/registration forms; ignore learn/quiz content.\n"
    "- Each candidate shows `samples=` (real values). Use them to break ties and to AVOID "
    "placeholder/garbage columns: if a column's samples are clearly synthetic fillers "
    "(e.g. 'sample-101', 'sample-51') or otherwise don't match the measure's expected shape "
    "(a numeric measure needs numeric-looking samples), do NOT pick it — prefer the column "
    "whose samples actually look like the measure, or status=absent if none do."
)


def _clinical_entry_candidates(candidates: list[FieldCandidate]) -> list[FieldCandidate]:
    """Shortlist the resolver ranks over: real entry questions in case-bearing (clinical)
    forms, deduped by column (prefer the one carrying a human label). This strips the
    learn/assessment noise (no case_type) and the DataBindOnly/Trigger copies."""
    best: dict[str, FieldCandidate] = {}
    for c in candidates:
        if not c.is_entry or not c.case_type:
            continue
        cur = best.get(c.column)
        if cur is None or (not cur.label and c.label):
            best[c.column] = c
    return list(best.values())[:_MAX_SHORTLIST]


def _build_messages(measure: CanonicalMeasureSpec, shortlist: list[FieldCandidate]) -> list:
    lines = [
        f"- column={c.column} | type={c.type} | form={c.form_name}/{c.module_name} "
        f"| label={c.label!r} | samples={list(c.samples)}"
        for c in shortlist
    ]
    human = (
        f"Measure: {measure.name}\n"
        f"Kind: {measure.kind}\n"
        f"Description: {measure.description}\n\n"
        f"Candidate fields ({len(shortlist)}):\n" + "\n".join(lines)
    )
    return [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=human)]


def _default_client() -> Any:
    """ChatAnthropic with forced structured output to ``_ResolutionResult``."""
    return ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL, temperature=0
    ).with_structured_output(_ResolutionResult)


async def resolve_measure(
    measure: CanonicalMeasureSpec,
    candidates: list[FieldCandidate],
    *,
    model_client: Any = None,
) -> MeasureResolution:
    """Resolve one canonical measure to a per-opp SQL expression over ``stg_visits``.

    ``model_client`` is any object with an async ``ainvoke(messages)`` returning a
    ``_ResolutionResult`` (the default forces Anthropic structured output); inject a fake in
    tests. Absence and low confidence are explicit, never silent.
    """
    shortlist = _clinical_entry_candidates(candidates)
    if not shortlist:
        return MeasureResolution(
            measure=measure.name,
            column=None,
            source_path=None,
            sql_expression=None,
            confidence=0.0,
            status="absent",
            matched_label="",
            reason="no clinical entry fields in this app",
        )

    client = model_client if model_client is not None else _default_client()
    result: _ResolutionResult = await client.ainvoke(_build_messages(measure, shortlist))

    if result.status == "absent" or not result.column:
        return MeasureResolution(
            measure=measure.name,
            column=None,
            source_path=None,
            sql_expression=None,
            confidence=result.confidence,
            status="absent",
            matched_label="",
            reason=result.reason,
        )

    # Match by column (what the SQL reads) to recover the source xpath + label for provenance.
    matched = next((c for c in shortlist if c.column == result.column), None)
    if matched is None:
        # The LLM returned a column not in the candidate set — hallucinated output.
        # Treat as absent rather than letting an untrusted column reach generated SQL.
        return MeasureResolution(
            measure=measure.name,
            column=None,
            source_path=None,
            sql_expression=None,
            confidence=result.confidence,
            status="absent",
            matched_label="",
            reason="resolved column not in candidate set",
        )
    status: Literal["resolved", "low_confidence", "absent"] = (
        "low_confidence" if result.confidence < _LOW_CONFIDENCE else "resolved"
    )
    return MeasureResolution(
        measure=measure.name,
        column=result.column,
        source_path=matched.path,
        sql_expression=result.sql_expression,
        confidence=result.confidence,
        status=status,
        matched_label=matched.label,
        reason=result.reason,
    )
