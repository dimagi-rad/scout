"""
Tests for apps.evals.services.scorecard.summarize().

All tests are deterministic — no live LLM, Cube API, or agent calls.
EvalRun objects are built using the Django ORM against the test DB.

Test DB is on port 5435.  Run with:

    DATABASE_URL=postgresql://platform:devpassword@localhost:5435/agent_platform \\
    DJANGO_SETTINGS_MODULE=config.settings.test \\
    DJANGO_SECRET_KEY=test-secret \\
    uv run pytest tests/test_eval_scorecard.py -v
"""

from __future__ import annotations

import pytest

from apps.evals.models import EvalRun, GoldenQuery
from apps.evals.services.scorecard import summarize

# Sentinel: distinguishes "caller wants the default value" from "caller explicitly passes None"
_UNSET = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_eval_run(
    golden_query: GoldenQuery,
    *,
    result_match: bool | None = True,
    free_sql_ms: int | None = 200,
    cube_ms: int | None = 50,
    cube_result: object = _UNSET,
    used_preaggregation: bool = False,
) -> EvalRun:
    """Synchronous helper to create an EvalRun instance in the DB.

    Pass ``cube_result=None`` explicitly to store a database NULL (unanswerable).
    Omit cube_result (or use the default) to get a valid ``[{"count": 42}]`` result.
    """
    if cube_result is _UNSET:
        cube_result = [{"count": 42}]
    return EvalRun.objects.create(
        golden_query=golden_query,
        workspace=golden_query.workspace,
        free_sql="SELECT 1",
        free_sql_result=[{"count": 42}],
        free_sql_ms=free_sql_ms,
        cube_query="MEASURE(things.count)",
        cube_result=cube_result,
        cube_ms=cube_ms,
        result_match=result_match,
        match_confidence=1.0 if result_match else 0.0,
        semantic_equivalence="exact" if result_match else "failed",
        used_preaggregation=used_preaggregation,
    )


# ---------------------------------------------------------------------------
# Empty-input tolerance
# ---------------------------------------------------------------------------


def test_summarize_empty_returns_safe_defaults():
    """summarize([]) returns a fully-structured dict with None/0 values."""
    card = summarize([])

    assert card["total_runs"] == 0
    assert card["correctness"]["pct"] is None
    assert card["correctness"]["correct"] == 0
    assert card["correctness"]["total"] == 0
    assert card["consistency"]["mean_agreement"] is None
    assert card["consistency"]["by_question"] == {}
    assert card["latency"]["free_sql_ms"]["mean"] is None
    assert card["latency"]["free_sql_ms"]["samples"] == 0
    assert card["latency"]["cube_ms"]["mean"] is None
    assert card["latency"]["cube_ms"]["samples"] == 0
    assert card["cube_answerable"]["count"] == 0
    assert card["cube_answerable"]["total_questions"] == 0
    assert card["preagg_speedup"]["speedup_x"] is None


# ---------------------------------------------------------------------------
# Correctness %
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_correctness_all_match(workspace):
    """100% correctness when all runs have result_match=True."""
    gq = GoldenQuery.objects.create(
        workspace=workspace, title="All match", question="Q?"
    )
    runs = [_make_eval_run(gq, result_match=True) for _ in range(5)]
    card = summarize(runs)

    assert card["correctness"]["correct"] == 5
    assert card["correctness"]["total"] == 5
    assert card["correctness"]["pct"] == pytest.approx(100.0)


@pytest.mark.django_db(transaction=True)
def test_correctness_mixed(workspace):
    """Correctness % is computed correctly when some runs fail."""
    gq = GoldenQuery.objects.create(
        workspace=workspace, title="Mixed", question="Q?"
    )
    runs = [
        _make_eval_run(gq, result_match=True),
        _make_eval_run(gq, result_match=False),
        _make_eval_run(gq, result_match=True),
        _make_eval_run(gq, result_match=False),
    ]
    card = summarize(runs)

    assert card["correctness"]["correct"] == 2
    assert card["correctness"]["total"] == 4
    assert card["correctness"]["pct"] == pytest.approx(50.0)


@pytest.mark.django_db(transaction=True)
def test_correctness_null_result_match_excluded(workspace):
    """Runs with result_match=None are excluded from the correctness denominator."""
    gq = GoldenQuery.objects.create(
        workspace=workspace, title="Null match", question="Q?"
    )
    runs = [
        _make_eval_run(gq, result_match=True),
        _make_eval_run(gq, result_match=None),
    ]
    card = summarize(runs)

    # Only the one True run counts; the None run is excluded
    assert card["correctness"]["correct"] == 1
    assert card["correctness"]["total"] == 1
    assert card["correctness"]["pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Consistency
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_consistency_all_agree(workspace):
    """3/3 runs agreeing → agreement_rate = 1.0."""
    gq = GoldenQuery.objects.create(
        workspace=workspace, title="Consistent", question="Q?"
    )
    runs = [_make_eval_run(gq, result_match=True) for _ in range(3)]
    card = summarize(runs)

    gq_key = str(gq.pk)
    assert card["consistency"]["by_question"][gq_key] == pytest.approx(1.0)
    assert card["consistency"]["mean_agreement"] == pytest.approx(1.0)


@pytest.mark.django_db(transaction=True)
def test_consistency_partial_disagreement(workspace):
    """2/3 runs agreeing → agreement_rate ≈ 0.667."""
    gq = GoldenQuery.objects.create(
        workspace=workspace, title="Partial agree", question="Q?"
    )
    runs = [
        _make_eval_run(gq, result_match=True),
        _make_eval_run(gq, result_match=True),
        _make_eval_run(gq, result_match=False),
    ]
    card = summarize(runs)

    gq_key = str(gq.pk)
    assert card["consistency"]["by_question"][gq_key] == pytest.approx(2 / 3, rel=1e-3)


@pytest.mark.django_db(transaction=True)
def test_consistency_multiple_questions(workspace):
    """mean_agreement averages per-question agreement rates."""
    gq1 = GoldenQuery.objects.create(workspace=workspace, title="Q1", question="Q1?")
    gq2 = GoldenQuery.objects.create(workspace=workspace, title="Q2", question="Q2?")

    # Q1: 2/2 agree → 1.0
    runs_q1 = [_make_eval_run(gq1, result_match=True) for _ in range(2)]
    # Q2: 1/2 agree → 0.5
    runs_q2 = [
        _make_eval_run(gq2, result_match=True),
        _make_eval_run(gq2, result_match=False),
    ]
    card = summarize(runs_q1 + runs_q2)

    # mean of 1.0 and 0.5 = 0.75
    assert card["consistency"]["mean_agreement"] == pytest.approx(0.75, rel=1e-3)


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_latency_means(workspace):
    """Mean free_sql_ms and cube_ms are computed correctly."""
    gq = GoldenQuery.objects.create(workspace=workspace, title="Latency", question="Q?")
    runs = [
        _make_eval_run(gq, free_sql_ms=100, cube_ms=20),
        _make_eval_run(gq, free_sql_ms=200, cube_ms=40),
        _make_eval_run(gq, free_sql_ms=300, cube_ms=60),
    ]
    card = summarize(runs)

    assert card["latency"]["free_sql_ms"]["mean"] == pytest.approx(200.0)
    assert card["latency"]["free_sql_ms"]["samples"] == 3
    assert card["latency"]["cube_ms"]["mean"] == pytest.approx(40.0)
    assert card["latency"]["cube_ms"]["samples"] == 3


@pytest.mark.django_db(transaction=True)
def test_latency_nulls_excluded(workspace):
    """Null latency values are excluded from mean computation."""
    gq = GoldenQuery.objects.create(workspace=workspace, title="Null lat", question="Q?")
    runs = [
        _make_eval_run(gq, free_sql_ms=100, cube_ms=None),
        _make_eval_run(gq, free_sql_ms=None, cube_ms=50),
    ]
    card = summarize(runs)

    assert card["latency"]["free_sql_ms"]["mean"] == pytest.approx(100.0)
    assert card["latency"]["free_sql_ms"]["samples"] == 1
    assert card["latency"]["cube_ms"]["mean"] == pytest.approx(50.0)
    assert card["latency"]["cube_ms"]["samples"] == 1


# ---------------------------------------------------------------------------
# Cube answerability
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_cube_answerable_count(workspace):
    """Questions with a non-null, non-error cube_result count as answerable."""
    gq1 = GoldenQuery.objects.create(workspace=workspace, title="Answerable", question="Q1?")
    gq2 = GoldenQuery.objects.create(workspace=workspace, title="Not answerable", question="Q2?")
    gq3 = GoldenQuery.objects.create(workspace=workspace, title="Error result", question="Q3?")

    runs = [
        # gq1: cube answered fine
        _make_eval_run(gq1, cube_result=[{"count": 42}]),
        # gq2: cube result is None
        _make_eval_run(gq2, cube_result=None),
        # gq3: cube returned an error envelope
        _make_eval_run(gq3, cube_result={"error": "Cube SQL timeout", "code": "TIMEOUT"}),
    ]
    card = summarize(runs)

    assert card["cube_answerable"]["count"] == 1
    assert card["cube_answerable"]["total_questions"] == 3


@pytest.mark.django_db(transaction=True)
def test_cube_answerable_at_least_one_run(workspace):
    """A question is answerable if ANY run has a valid cube_result."""
    gq = GoldenQuery.objects.create(workspace=workspace, title="Mixed cube", question="Q?")
    runs = [
        # First run failed
        _make_eval_run(gq, cube_result=None),
        # Second run succeeded
        _make_eval_run(gq, cube_result=[{"count": 10}]),
    ]
    card = summarize(runs)

    assert card["cube_answerable"]["count"] == 1
    assert card["cube_answerable"]["total_questions"] == 1


# ---------------------------------------------------------------------------
# Pre-aggregation speedup
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_preagg_speedup_calculated(workspace):
    """speedup_x = without_preagg_mean / with_preagg_mean."""
    gq = GoldenQuery.objects.create(workspace=workspace, title="Preagg", question="Q?")
    runs = [
        # Without pre-agg: slow (300 ms)
        _make_eval_run(gq, cube_ms=300, used_preaggregation=False),
        _make_eval_run(gq, cube_ms=300, used_preaggregation=False),
        # With pre-agg: fast (50 ms)
        _make_eval_run(gq, cube_ms=50, used_preaggregation=True),
        _make_eval_run(gq, cube_ms=50, used_preaggregation=True),
    ]
    card = summarize(runs)

    pa = card["preagg_speedup"]
    assert pa["with_preagg_mean_ms"] == pytest.approx(50.0)
    assert pa["without_preagg_mean_ms"] == pytest.approx(300.0)
    assert pa["speedup_x"] == pytest.approx(6.0, rel=1e-3)


@pytest.mark.django_db(transaction=True)
def test_preagg_speedup_none_when_missing_one_bucket(workspace):
    """speedup_x is None when only one of the two buckets has data."""
    gq = GoldenQuery.objects.create(workspace=workspace, title="Only no-preagg", question="Q?")
    runs = [
        _make_eval_run(gq, cube_ms=200, used_preaggregation=False),
    ]
    card = summarize(runs)

    assert card["preagg_speedup"]["speedup_x"] is None
    assert card["preagg_speedup"]["with_preagg_mean_ms"] is None
    assert card["preagg_speedup"]["without_preagg_mean_ms"] == pytest.approx(200.0)


@pytest.mark.django_db(transaction=True)
def test_preagg_speedup_all_zero_preagg_ms(workspace):
    """speedup_x is None when with-preagg mean_ms would be zero (avoids ZeroDivision)."""
    gq = GoldenQuery.objects.create(workspace=workspace, title="Zero preagg ms", question="Q?")
    runs = [
        _make_eval_run(gq, cube_ms=0, used_preaggregation=True),
        _make_eval_run(gq, cube_ms=200, used_preaggregation=False),
    ]
    card = summarize(runs)

    # with_preagg_mean_ms == 0 → cannot compute speedup
    assert card["preagg_speedup"]["speedup_x"] is None


# ---------------------------------------------------------------------------
# Total runs
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_total_runs_count(workspace):
    """total_runs equals the length of the input list."""
    gq = GoldenQuery.objects.create(workspace=workspace, title="Count", question="Q?")
    runs = [_make_eval_run(gq) for _ in range(7)]
    card = summarize(runs)

    assert card["total_runs"] == 7
