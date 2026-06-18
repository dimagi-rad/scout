"""
Tests for apps.evals.services.judge and apps.evals.services.runner.

All tests are deterministic — no live LLM, Cube API, or agent calls.
The model_client, free_path, cube_path, and judge are all injected fakes.

Test DB is on port 5435.  Run with:

    DATABASE_URL=postgresql://platform:devpassword@localhost:5435/agent_platform \
    DJANGO_SETTINGS_MODULE=config.settings.test \
    DJANGO_SECRET_KEY=test-secret \
    uv run pytest tests/test_eval_runner.py -v
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from asgiref.sync import sync_to_async

from apps.evals.models import EvalRun, GoldenQuery
from apps.evals.services.judge import judge_equivalence
from apps.evals.services.runner import run_eval

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _fake_llm_client(verdict: dict) -> MagicMock:
    """Return a mock client whose ainvoke returns the given verdict as JSON."""
    client = MagicMock()
    response = MagicMock()
    response.content = json.dumps(verdict)
    client.ainvoke = AsyncMock(return_value=response)
    return client


async def _make_free_path(sql: str = "SELECT 1", result=None, ms: int = 10):
    """Factory for a fake free_path coroutine."""
    _result = result if result is not None else [{"count": 42}]

    async def _free_path(question: str) -> dict:
        return {"sql": sql, "result": _result, "ms": ms}

    return _free_path


async def _make_cube_path(query: str = "MEASURE(users.count)", result=None, ms: int = 5):
    """Factory for a fake cube_path coroutine."""
    _result = result if result is not None else [{"count": 42}]

    async def _cube_path(question: str) -> dict:
        return {"query": query, "result": _result, "ms": ms}

    return _cube_path


async def _fake_judge(
    question: str, free_result, cube_result
) -> dict:
    """Fake judge that always returns an exact match."""
    return {"match": True, "confidence": 1.0, "equivalence": "exact"}


# ---------------------------------------------------------------------------
# judge_equivalence — deterministic (exact) path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_exact_no_llm_call():
    """Identical result sets are detected without calling the LLM."""
    client = _fake_llm_client({"match": True, "confidence": 0.9, "equivalence": "approximate"})

    verdict = await judge_equivalence(
        "How many users?",
        free_result=[{"count": 42}],
        cube_result=[{"Users.count": 42}],
        model_client=client,
    )

    assert verdict["match"] is True
    assert verdict["confidence"] == 1.0
    assert verdict["equivalence"] == "exact"
    # The LLM must NOT have been called
    client.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_judge_exact_order_independent():
    """Row order and column name differences don't affect deterministic exact match."""
    free = [{"a": 1}, {"a": 2}]
    cube = [{"X.a": "2"}, {"X.a": "1"}]  # different order, different col name, same values

    client = _fake_llm_client({"match": False, "confidence": 0.5, "equivalence": "failed"})
    verdict = await judge_equivalence("Q?", free, cube, model_client=client)

    assert verdict["equivalence"] == "exact"
    assert verdict["match"] is True
    client.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_judge_differing_calls_llm():
    """Differing result sets trigger the LLM judge."""
    llm_verdict = {"match": False, "confidence": 0.8, "equivalence": "failed"}
    client = _fake_llm_client(llm_verdict)

    verdict = await judge_equivalence(
        "How many users?",
        free_result=[{"count": 42}],
        cube_result=[{"count": 99}],
        model_client=client,
    )

    client.ainvoke.assert_called_once()
    assert verdict["match"] is False
    assert verdict["confidence"] == pytest.approx(0.8)
    assert verdict["equivalence"] == "failed"


@pytest.mark.asyncio
async def test_judge_approximate_from_llm():
    """LLM returning approximate match is passed through correctly."""
    llm_verdict = {"match": True, "confidence": 0.75, "equivalence": "approximate"}
    client = _fake_llm_client(llm_verdict)

    verdict = await judge_equivalence(
        "Revenue last month?",
        free_result=[{"revenue": 1000}],
        cube_result=[{"revenue": 999}],
        model_client=client,
    )

    assert verdict["match"] is True
    assert verdict["equivalence"] == "approximate"
    assert verdict["confidence"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_judge_llm_bad_json_falls_back_to_failed():
    """Unparseable LLM output falls back to failed verdict without raising."""
    client = MagicMock()
    response = MagicMock()
    response.content = "I cannot determine equivalence."
    client.ainvoke = AsyncMock(return_value=response)

    verdict = await judge_equivalence(
        "Q?",
        free_result=[{"x": 1}],
        cube_result=[{"x": 2}],
        model_client=client,
    )

    assert verdict["match"] is False
    assert verdict["equivalence"] == "failed"
    assert verdict["confidence"] == 0.0


@pytest.mark.asyncio
async def test_judge_empty_vs_empty_exact():
    """Two empty result sets are an exact match."""
    client = _fake_llm_client({"match": False, "confidence": 0.0, "equivalence": "failed"})
    verdict = await judge_equivalence("Nothing?", [], [], model_client=client)
    assert verdict["equivalence"] == "exact"
    client.ainvoke.assert_not_called()


# ---------------------------------------------------------------------------
# run_eval — persistence tests (requires DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_eval_persists_correct_run_count(workspace):
    """run_eval(runs=3) persists exactly 3 EvalRun rows."""
    gq = await sync_to_async(GoldenQuery.objects.create)(
        workspace=workspace,
        title="Count users",
        question="How many users are there?",
    )
    free_p = await _make_free_path()
    cube_p = await _make_cube_path()

    runs = await run_eval(
        gq,
        runs=3,
        free_path=free_p,
        cube_path=cube_p,
        judge=_fake_judge,
    )

    assert len(runs) == 3
    db_count = await sync_to_async(EvalRun.objects.filter(golden_query=gq).count)()
    assert db_count == 3


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_eval_fields_captured(workspace):
    """EvalRun rows carry correct sql, query, result, ms, and scoring fields."""
    gq = await sync_to_async(GoldenQuery.objects.create)(
        workspace=workspace,
        title="Revenue",
        question="Total revenue?",
    )
    free_result = [{"revenue": 1000}]
    cube_result = [{"revenue": 1000}]

    free_p = await _make_free_path(sql="SELECT SUM(r) FROM t", result=free_result, ms=120)
    cube_p = await _make_cube_path(query="MEASURE(orders.revenue)", result=cube_result, ms=45)

    runs = await run_eval(
        gq,
        runs=1,
        free_path=free_p,
        cube_path=cube_p,
        judge=_fake_judge,
    )

    run = runs[0]
    assert run.free_sql == "SELECT SUM(r) FROM t"
    assert run.free_sql_result == free_result
    assert run.free_sql_ms == 120
    assert run.cube_query == "MEASURE(orders.revenue)"
    assert run.cube_result == cube_result
    assert run.cube_ms == 45
    assert run.result_match is True
    assert run.match_confidence == pytest.approx(1.0)
    assert run.semantic_equivalence == "exact"
    assert run.workspace == workspace
    assert run.golden_query == gq


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_eval_use_preagg_true(workspace):
    """use_preagg=True is stored on all EvalRun rows."""
    gq = await sync_to_async(GoldenQuery.objects.create)(
        workspace=workspace,
        title="Preagg test",
        question="Any?",
    )
    free_p = await _make_free_path()
    cube_p = await _make_cube_path()

    runs = await run_eval(
        gq,
        runs=2,
        use_preagg=True,
        free_path=free_p,
        cube_path=cube_p,
        judge=_fake_judge,
    )

    assert all(r.used_preaggregation is True for r in runs)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_eval_use_preagg_false_default(workspace):
    """use_preagg defaults to False."""
    gq = await sync_to_async(GoldenQuery.objects.create)(
        workspace=workspace,
        title="No preagg",
        question="Any?",
    )
    free_p = await _make_free_path()
    cube_p = await _make_cube_path()

    runs = await run_eval(
        gq,
        runs=1,
        free_path=free_p,
        cube_path=cube_p,
        judge=_fake_judge,
    )

    assert runs[0].used_preaggregation is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_eval_judge_verdict_stored(workspace):
    """The judge's verdict is stored correctly on EvalRun."""
    gq = await sync_to_async(GoldenQuery.objects.create)(
        workspace=workspace,
        title="Mismatch",
        question="Any?",
    )
    free_p = await _make_free_path(result=[{"x": 1}])
    cube_p = await _make_cube_path(result=[{"x": 99}])

    async def _mismatch_judge(question, free_result, cube_result) -> dict:
        return {"match": False, "confidence": 0.3, "equivalence": "failed"}

    runs = await run_eval(
        gq,
        runs=1,
        free_path=free_p,
        cube_path=cube_p,
        judge=_mismatch_judge,
    )

    run = runs[0]
    assert run.result_match is False
    assert run.match_confidence == pytest.approx(0.3)
    assert run.semantic_equivalence == "failed"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_eval_latency_captured(workspace):
    """free_sql_ms and cube_ms are captured (from fake paths' ms fields)."""
    gq = await sync_to_async(GoldenQuery.objects.create)(
        workspace=workspace,
        title="Latency",
        question="Speed test?",
    )
    free_p = await _make_free_path(ms=250)
    cube_p = await _make_cube_path(ms=30)

    runs = await run_eval(
        gq,
        runs=1,
        free_path=free_p,
        cube_path=cube_p,
        judge=_fake_judge,
    )

    assert runs[0].free_sql_ms == 250
    assert runs[0].cube_ms == 30
