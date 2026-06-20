"""Propose likely cross-opp measures from the apps ("what analysis is most likely").

Emits the SAME CanonicalMeasureSpec the on-demand path uses, so the downstream engine
(resolve -> doubt-gate -> commit) is identical. This replaces the hardcoded STARTER_MEASURES.
"""

from __future__ import annotations

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from apps.transformations.services.measure_resolver import (
    CanonicalMeasureSpec,
    _clinical_entry_candidates,
)


class _Proposed(BaseModel):
    name: str = Field(description="snake_case slug")
    description: str
    kind: str = Field(description="'numeric' or 'rate'")


class _ProposedList(BaseModel):
    measures: list[_Proposed]


_SYSTEM = (
    "You design a starter analytics catalog for a clinical program spanning several apps. "
    "Given candidate fields (label + type) common across the apps, propose the measures an "
    "operations expert is most likely to want to compare across sites. Prefer clinical "
    "outcomes and delivery quality. Each measure: a snake_case name, a one-line description, "
    "and kind='numeric' (a value to average) or 'rate' (a boolean event averaged 0..1). "
    "Do not invent fields that aren't represented; propose only what the labels support."
)


def _default_client():
    return ChatAnthropic(model=settings.DEFAULT_LLM_MODEL, temperature=0).with_structured_output(
        _ProposedList
    )


async def propose_measures(candidates_by_opp, *, model_client=None, limit=8):
    """Propose likely cross-opp measures from the union of clinical entry candidates.

    Args:
        candidates_by_opp: dict mapping opp_id -> list[FieldCandidate]
        model_client: optional injectable LLM client (for tests); defaults to ChatAnthropic
        limit: maximum number of measures to propose (default 8)

    Returns:
        list[CanonicalMeasureSpec] — same type the on-demand define path uses
    """
    seen, lines = set(), []
    for cands in candidates_by_opp.values():
        for c in _clinical_entry_candidates(cands):
            if c.column in seen:
                continue
            seen.add(c.column)
            lines.append(f"- column={c.column} | type={c.type} | label={c.label!r}")

    client = model_client or _default_client()
    msgs = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(
            content="Candidate fields:\n" + "\n".join(lines) + f"\n\nPropose up to {limit} measures."
        ),
    ]
    result: _ProposedList = await client.ainvoke(msgs)
    return [
        CanonicalMeasureSpec(
            name=m.name,
            description=m.description,
            kind=("rate" if m.kind == "rate" else "numeric"),
        )
        for m in result.measures[:limit]
    ]
