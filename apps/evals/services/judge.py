"""
LLM-backed equivalence judge for the free-SQL vs Cube eval loop.

Public API
----------
    verdict = await judge_equivalence(
        question="How many active users?",
        free_result=[{"count": 42}],
        cube_result=[{"Users.count": 42}],
    )
    # verdict: {"match": bool, "confidence": float, "equivalence": str}

Two-phase approach
------------------
1. **Cheap deterministic check**: normalise both result sets (sort rows,
   normalise values to strings, preserve column-name identity) and compare.
   Column names are included in the key so ``{"x": 1}`` ≠ ``{"y": 1}``.
   Special case: a 1-row × 1-cell result ignores the column name (a lone
   scalar is unambiguous).  Identical normalised sets → ``exact`` match,
   confidence 1.0, no LLM call.

2. **LLM semantic judge**: when results don't trivially match, invoke
   ``model_client`` (injectable; defaults to ChatAnthropic using
   ``settings.DEFAULT_LLM_MODEL``) and parse its JSON verdict.

Injectable interface
--------------------
Pass ``model_client=`` for tests.  The interface is:
    ``await client.ainvoke(messages)``  →  response with ``.content: str``
    or fall back to sync ``client.invoke(messages)`` in a thread.

This is the same pattern used by ``apps/transformations/services/cube_model_generator.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from django.conf import settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default LLM client factory
# ---------------------------------------------------------------------------


def _default_model_client() -> Any:
    """Build the default ChatAnthropic client (matches agents/transformations convention)."""
    return ChatAnthropic(
        model=settings.DEFAULT_LLM_MODEL,
        max_tokens=1024,
    )


# ---------------------------------------------------------------------------
# LLM invocation helper (mirrors cube_model_generator._call_model)
# ---------------------------------------------------------------------------


async def _call_model(client: Any, messages: list) -> str:
    """Invoke model_client; supports async ainvoke and sync invoke (via thread)."""
    if hasattr(client, "ainvoke"):
        response = await client.ainvoke(messages)
    else:
        response = await asyncio.to_thread(client.invoke, messages)
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, list):
            return " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(content)
    return str(response)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _normalise_value(v: Any) -> str:
    """Convert a cell value to a canonical string for comparison.

    Strips trailing zeros from numeric strings so "42.0" == "42".
    """
    if v is None:
        return "null"
    s = str(v).strip()
    # If it looks like a number, normalise to avoid "42" vs "42.0" mismatches.
    try:
        f = float(s)
        # Use integer form when possible
        return str(int(f)) if f == int(f) else str(f)
    except (ValueError, OverflowError):
        return s


def _normalise_row(row: Any) -> frozenset[tuple[str, str]]:
    """Convert a row (dict or list) to a frozenset of (normalised_key, normalised_value) tuples.

    Column identity is preserved so that ``{"x": 1}`` and ``{"y": 1}`` are NOT
    considered equal — a column-name mismatch means the deterministic check does
    not short-circuit, and the LLM judge is invoked instead.

    Row-ordering across columns within one row is irrelevant (frozenset), so
    ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` are still equal.

    For positional results (list/tuple) the index is used as the key so column
    order is preserved.

    Special fast-path (caller responsibility): single-cell 1×1 results skip
    column-name comparison entirely (see ``_normalise_result``).
    """
    if isinstance(row, dict):
        return frozenset(
            (_normalise_value(k), _normalise_value(v)) for k, v in row.items()
        )
    if isinstance(row, (list, tuple)):
        return frozenset(
            (str(i), _normalise_value(v)) for i, v in enumerate(row)
        )
    # Scalar — treat as a single-value row with a placeholder key
    return frozenset([("_", _normalise_value(row))])


def _normalise_result(result: Any) -> list[frozenset[tuple[str, str]]]:
    """Return a sorted list of normalised rows for order-independent comparison.

    Each row becomes a ``frozenset`` of ``(normalised_key, normalised_value)``
    pairs so column identity is preserved.  Sorting the list makes row order
    irrelevant for the deterministic comparison.

    Special 1×1 fast path
    ---------------------
    When the entire result is exactly **one row with exactly one cell**, column
    names are replaced with a canonical placeholder (``"_"``) before returning.
    A lone scalar aggregate (e.g. ``[{"count": 42}]`` vs ``[{"Users.count": 42}]``)
    is unambiguous regardless of the column label, so the deterministic check
    fires in this case even though column names differ.
    """
    if result is None:
        return []
    if isinstance(result, list):
        rows = result
    elif isinstance(result, dict):
        # Some paths wrap rows under a "rows" key (MCP envelope)
        rows = result.get("rows", [result])
    else:
        rows = [result]

    normalised = [_normalise_row(r) for r in rows]

    # 1x1 fast path: one row, one cell -> strip column name so that
    # {"count": 42} and {"Users.count": 42} both become {("_", "42")}.
    if len(normalised) == 1 and len(normalised[0]) == 1:
        ((_key, value),) = normalised[0]
        normalised = [frozenset([("_", value)])]

    return sorted(normalised, key=lambda s: sorted(s))


# ---------------------------------------------------------------------------
# LLM judge prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a data-result equivalence judge.  Given two result sets that answered the
same analytical question, decide whether they are semantically equivalent.

Respond with ONLY a JSON object (no prose, no markdown fences) in this exact shape:
{
  "match": true|false,
  "confidence": 0.0..1.0,
  "equivalence": "exact"|"approximate"|"failed"
}

Guidelines:
- "exact": same values, possibly different formatting or column names.
- "approximate": same trend/answer but minor numerical differences (rounding, currency).
- "failed": clearly different answers or one path errored.
- confidence 1.0 = certain; 0.5 = unsure; 0.0 = opposite of what you'd expect.
"""


def _build_judge_prompt(question: str, free_result: Any, cube_result: Any) -> str:
    def _repr(r: Any) -> str:
        try:
            return json.dumps(r, default=str)[:2000]
        except Exception:
            return str(r)[:2000]

    return (
        f"Question: {question}\n\n"
        f"Free-SQL result:\n{_repr(free_result)}\n\n"
        f"Cube semantic-layer result:\n{_repr(cube_result)}\n\n"
        "Are these equivalent?  Respond with the JSON object only."
    )


# ---------------------------------------------------------------------------
# Parse LLM verdict defensively
# ---------------------------------------------------------------------------


def _parse_verdict(raw: str) -> dict:
    """Parse the LLM's JSON verdict; fall back to a 'failed' verdict on error."""
    raw = raw.strip()
    # Strip possible markdown fences
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()
    try:
        data = json.loads(raw)
        match = bool(data.get("match", False))
        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        equivalence = data.get("equivalence", "failed")
        if equivalence not in ("exact", "approximate", "failed"):
            equivalence = "failed"
        return {"match": match, "confidence": confidence, "equivalence": equivalence}
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("judge_equivalence: could not parse LLM verdict: %r", raw)
        return {"match": False, "confidence": 0.0, "equivalence": "failed"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def judge_equivalence(
    question: str,
    free_result: Any,
    cube_result: Any,
    *,
    model_client: Any = None,
) -> dict:
    """Judge whether ``free_result`` and ``cube_result`` answer ``question`` equivalently.

    Phase 1 — Deterministic normalised compare
    ------------------------------------------
    Both result sets are normalised (column-identity preserved, order-independent)
    and compared.  Column names are kept so that ``[{"x": 1}]`` vs ``[{"y": 1}]``
    does NOT short-circuit to "exact".  Exception: if both results are exactly
    1 row × 1 cell, column names are ignored (a lone scalar aggregate like
    ``[{"count": 42}]`` vs ``[{"Users.count": 42}]`` is unambiguous).  When the
    deterministic check fires → return ``exact`` / match=True / confidence=1.0
    without calling the LLM.

    Phase 2 — LLM semantic judge
    ----------------------------
    When the deterministic check fails, invoke ``model_client`` (defaults to
    ChatAnthropic(settings.DEFAULT_LLM_MODEL)) with a structured prompt and
    parse the JSON verdict.

    Args:
        question: The natural-language question that was answered.
        free_result: Result from the free-SQL path (list of rows, dict, etc.).
        cube_result: Result from the Cube semantic-layer path.
        model_client: Injectable LLM client for testing.  The interface is
            ``await client.ainvoke(messages)`` → response with ``.content: str``.
            Defaults to ``ChatAnthropic(settings.DEFAULT_LLM_MODEL)``.

    Returns:
        Dict with keys:
            match (bool): True when results are judged equivalent.
            confidence (float): 0.0–1.0 confidence in the match verdict.
            equivalence (str): "exact" | "approximate" | "failed".
    """
    # ------------------------------------------------------------------
    # Phase 1: cheap deterministic compare
    # ------------------------------------------------------------------
    free_norm = _normalise_result(free_result)
    cube_norm = _normalise_result(cube_result)

    if free_norm == cube_norm:
        return {"match": True, "confidence": 1.0, "equivalence": "exact"}

    # ------------------------------------------------------------------
    # Phase 2: LLM semantic judge
    # ------------------------------------------------------------------
    client = model_client if model_client is not None else _default_model_client()

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=_build_judge_prompt(question, free_result, cube_result)),
    ]
    raw = await _call_model(client, messages)
    return _parse_verdict(raw)
