"""
Scorecard computation for the free-SQL vs Cube adoption eval loop.

Pure function — no DB access, no LLM calls, fully deterministic.

Public API
----------
    from apps.evals.services.scorecard import summarize

    card = summarize(eval_runs)
    # card is a structured dict; see summarize() docstring for full schema.

Metrics
-------
Correctness
    % of EvalRun rows where result_match=True.

Consistency
    For each GoldenQuery, do the N runs agree on the result_match verdict?
    Reported as a per-query agreement rate (0.0–1.0) and the overall mean.

Mean latency
    Mean free_sql_ms and mean cube_ms across all runs (nulls excluded).

Cube answerability
    # of unique GoldenQuery IDs for which at least one run returned a
    non-null cube_result that is not an error envelope.

Pre-aggregation speedup
    Compare mean cube_ms for runs where used_preaggregation=True vs False.
    Returns None values when one or both buckets are empty.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from apps.evals.models import EvalRun


def _is_cube_error(result: Any) -> bool:
    """Return True if cube_result looks like an error envelope (not real data)."""
    if result is None:
        return True
    # MCP error envelope: {"error": ..., "code": ...}
    return isinstance(result, dict) and ("error" in result or "code" in result)


def summarize(eval_runs: list[EvalRun]) -> dict:
    """Compute a structured scorecard from a list of EvalRun instances.

    This is a pure function: it reads only the fields already loaded on the
    EvalRun objects and performs no DB or LLM calls.

    Args:
        eval_runs: List of EvalRun model instances (may be empty).

    Returns:
        A dict with the following shape:

        {
            "total_runs": int,
            "correctness": {
                "correct": int,          # runs with result_match=True
                "total": int,            # runs with a non-null result_match
                "pct": float | None,     # percentage correct (0–100), or None if empty
            },
            "consistency": {
                "by_question": {
                    "<golden_query_id>": float,   # agreement rate 0.0–1.0
                    ...
                },
                "mean_agreement": float | None,   # average across all questions
            },
            "latency": {
                "free_sql_ms": {
                    "mean": float | None,
                    "samples": int,
                },
                "cube_ms": {
                    "mean": float | None,
                    "samples": int,
                },
            },
            "cube_answerable": {
                "count": int,            # # unique GoldenQueries Cube could answer
                "total_questions": int,  # total unique GoldenQuery IDs seen
            },
            "preagg_speedup": {
                "with_preagg_mean_ms": float | None,
                "without_preagg_mean_ms": float | None,
                "speedup_x": float | None,   # without / with (>1 means preagg is faster)
            },
        }
    """
    if not eval_runs:
        return {
            "total_runs": 0,
            "correctness": {"correct": 0, "total": 0, "pct": None},
            "consistency": {"by_question": {}, "mean_agreement": None},
            "latency": {
                "free_sql_ms": {"mean": None, "samples": 0},
                "cube_ms": {"mean": None, "samples": 0},
            },
            "cube_answerable": {"count": 0, "total_questions": 0},
            "preagg_speedup": {
                "with_preagg_mean_ms": None,
                "without_preagg_mean_ms": None,
                "speedup_x": None,
            },
        }

    total_runs = len(eval_runs)

    # ------------------------------------------------------------------
    # Correctness
    # ------------------------------------------------------------------
    result_match_values = [r.result_match for r in eval_runs if r.result_match is not None]
    correct_count = sum(1 for m in result_match_values if m is True)
    total_with_verdict = len(result_match_values)
    correctness_pct: float | None = (
        (correct_count / total_with_verdict * 100.0) if total_with_verdict > 0 else None
    )

    # ------------------------------------------------------------------
    # Consistency (per GoldenQuery agreement rate)
    # ------------------------------------------------------------------
    by_question: dict[str, list[bool]] = defaultdict(list)
    for r in eval_runs:
        if r.result_match is not None:
            gq_key = str(r.golden_query_id)
            by_question[gq_key].append(bool(r.result_match))

    consistency_by_question: dict[str, float] = {}
    for gq_id, verdicts in by_question.items():
        if not verdicts:
            continue
        # Agreement rate: share of verdicts that equal the majority verdict
        majority = statistics.mode(verdicts)
        agreement = sum(1 for v in verdicts if v == majority) / len(verdicts)
        consistency_by_question[gq_id] = round(agreement, 4)

    mean_agreement: float | None = (
        statistics.mean(consistency_by_question.values())
        if consistency_by_question
        else None
    )

    # ------------------------------------------------------------------
    # Latency
    # ------------------------------------------------------------------
    free_ms_values = [r.free_sql_ms for r in eval_runs if r.free_sql_ms is not None]
    cube_ms_values = [r.cube_ms for r in eval_runs if r.cube_ms is not None]

    free_mean: float | None = statistics.mean(free_ms_values) if free_ms_values else None
    cube_mean: float | None = statistics.mean(cube_ms_values) if cube_ms_values else None

    # ------------------------------------------------------------------
    # Cube answerability
    # ------------------------------------------------------------------
    all_question_ids: set[str] = {str(r.golden_query_id) for r in eval_runs}
    cube_answerable_ids: set[str] = {
        str(r.golden_query_id)
        for r in eval_runs
        if not _is_cube_error(r.cube_result)
    }

    # ------------------------------------------------------------------
    # Pre-aggregation speedup
    # ------------------------------------------------------------------
    preagg_ms = [r.cube_ms for r in eval_runs if r.used_preaggregation and r.cube_ms is not None]
    no_preagg_ms = [
        r.cube_ms for r in eval_runs if not r.used_preaggregation and r.cube_ms is not None
    ]

    with_preagg_mean: float | None = statistics.mean(preagg_ms) if preagg_ms else None
    without_preagg_mean: float | None = statistics.mean(no_preagg_ms) if no_preagg_ms else None

    speedup_x: float | None = None
    if with_preagg_mean is not None and without_preagg_mean is not None and with_preagg_mean > 0:
        speedup_x = round(without_preagg_mean / with_preagg_mean, 3)

    return {
        "total_runs": total_runs,
        "correctness": {
            "correct": correct_count,
            "total": total_with_verdict,
            "pct": round(correctness_pct, 2) if correctness_pct is not None else None,
        },
        "consistency": {
            "by_question": consistency_by_question,
            "mean_agreement": round(mean_agreement, 4) if mean_agreement is not None else None,
        },
        "latency": {
            "free_sql_ms": {
                "mean": round(free_mean, 2) if free_mean is not None else None,
                "samples": len(free_ms_values),
            },
            "cube_ms": {
                "mean": round(cube_mean, 2) if cube_mean is not None else None,
                "samples": len(cube_ms_values),
            },
        },
        "cube_answerable": {
            "count": len(cube_answerable_ids),
            "total_questions": len(all_question_ids),
        },
        "preagg_speedup": {
            "with_preagg_mean_ms": round(with_preagg_mean, 2) if with_preagg_mean is not None else None,
            "without_preagg_mean_ms": (
                round(without_preagg_mean, 2) if without_preagg_mean is not None else None
            ),
            "speedup_x": speedup_x,
        },
    }
