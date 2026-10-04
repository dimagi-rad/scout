"""Unit tests for the artifact batch-query use case shared by View Data and runtime checks."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.db import OperationalError
from freezegun import freeze_time

from apps.artifacts.services.query_batch import (
    execute_artifact_plan,
    plan_artifact_queries,
    plan_story_queries,
)
from apps.common.capacity import CapacityResource
from apps.semantic.services.query import CAPACITY_EXHAUSTED_CATEGORY
from apps.semantic.services.query_outcomes import query_error
from apps.semantic.services.query_readiness import QuerySurfaceReadiness
from tests.test_artifact_date_resolution import story

WORKSPACE = SimpleNamespace(id="workspace")


def _artifact(data=None, semantic_queries=None):
    return SimpleNamespace(data=data, semantic_queries=semantic_queries or [])


@pytest.mark.parametrize("compare", [False, True])
def test_view_data_and_checks_plan_the_same_story_queries(compare):
    doc = story(compare=compare)
    with freeze_time("2026-09-16T13:00:00Z"):
        viewed = plan_artifact_queries(_artifact({"story_doc": doc}, [{"name": "stale"}]))
        checked = plan_story_queries(doc)
    assert viewed == checked
    assert [q.name for q in viewed.queries] == (
        ["q.sessions", "q.sessions_previous"] if compare else ["q.sessions"]
    )
    assert viewed.query_context["as_of"].startswith("2026-09-16T13:00:00")


@pytest.mark.parametrize(
    "data",
    [None, {}, {"story_doc": {"blocks": []}}, {"story_doc": "x"}],
    ids=["no_data", "no_doc", "empty_doc", "non_dict_doc"],
)
def test_without_bound_queries_view_data_runs_stored_queries_without_a_context(data):
    stored = [{"name": "a", "measures": ["visits.count"]}, None]
    plan = plan_artifact_queries(_artifact(data, stored))
    assert plan.entries == stored
    assert plan.query_context is None
    assert [q.name for q in plan.queries] == ["a", "semantic_query_1"]
    assert plan.queries[1].spec is None


@pytest.mark.asyncio
async def test_row_limit_applies_to_execution_without_changing_the_plan(monkeypatch):
    stored = [{"name": "a", "measures": ["a.count"]}, {"name": "b", "measures": ["b"], "limit": 5}]
    plan = plan_artifact_queries(_artifact(None, copy.deepcopy(stored)))
    run = AsyncMock(return_value={"columns": [], "rows": []})
    monkeypatch.setattr("apps.artifacts.services.query_batch.run_semantic_query", run)

    batch = await execute_artifact_plan(plan, WORKSPACE, user_id="u", row_limit=50, concurrency=1)

    assert [call.args[1] for call in run.await_args_list] == [
        {"measures": ["a.count"], "limit": 50},
        {"measures": ["b"], "limit": 5},
    ]
    assert all(call.args[0] is WORKSPACE for call in run.await_args_list)
    assert all(call.kwargs["user_id"] == "u" for call in run.await_args_list)
    assert [o.executed for o in batch.outcomes] == [c.args[1] for c in run.await_args_list]
    # The cache key is built from the plan, so execution must not mutate it.
    assert plan.entries == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 3])
async def test_execution_is_bounded_and_keeps_plan_order(monkeypatch, concurrency):
    active = peak = 0

    async def run(_workspace, query, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01 if query["measures"][0] == "q0" else 0)
        active -= 1
        return {"rows": [[query["measures"][0]]]}

    monkeypatch.setattr("apps.artifacts.services.query_batch.run_semantic_query", run)
    plan = plan_artifact_queries(
        _artifact(None, [{"name": f"q{i}", "measures": [f"q{i}"]} for i in range(7)])
    )
    batch = await execute_artifact_plan(
        plan, WORKSPACE, user_id="", row_limit=None, concurrency=concurrency
    )
    assert peak == concurrency
    assert [o.result["rows"][0][0] for o in batch.outcomes] == [f"q{i}" for i in range(7)]


@pytest.mark.asyncio
async def test_readiness_is_inspected_once_per_batch_over_valid_queries(monkeypatch):
    async def run(_workspace, query, *, readiness, **kwargs):
        await readiness.surface()
        return {"rows": []}

    monkeypatch.setattr("apps.artifacts.services.query_batch.run_semantic_query", run)
    plan = plan_artifact_queries(
        _artifact(None, [{"name": "a", "measures": ["a"]}, None, {"measures": ["b"]}])
    )
    with patch(
        "apps.semantic.services.query_outcomes.query_surface_readiness",
        new=AsyncMock(return_value=QuerySurfaceReadiness({"queryable": True})),
    ) as inspect:
        await execute_artifact_plan(plan, WORKSPACE, user_id="", row_limit=None, concurrency=4)
    inspect.assert_awaited_once()
    assert inspect.await_args.args[1] == [{"measures": ["a"]}, {"measures": ["b"]}]


@pytest.mark.asyncio
async def test_outcomes_classify_success_failure_capacity_and_raised_errors(monkeypatch):
    bug = RuntimeError("bug")
    outcomes = {
        "ok": {"rows": [[1]]},
        "explicit_ok": {"success": True, "rows": []},
        "failed": {"success": False, "error": {"message": "nope"}},
        "error_only": {"error": "nope"},
        "cube_full": query_error("CONNECTION_ERROR", "full", category=CAPACITY_EXHAUSTED_CATEGORY),
        "db_full": OperationalError("FATAL:  sorry, too many clients already"),
        "bug": bug,
    }

    async def run(_workspace, query, **kwargs):
        outcome = outcomes[query["measures"][0]]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("apps.artifacts.services.query_batch.run_semantic_query", run)
    plan = plan_artifact_queries(
        _artifact(None, [{"name": key, "measures": [key]} for key in outcomes] + ["bad"])
    )
    batch = await execute_artifact_plan(plan, WORKSPACE, user_id="", row_limit=None, concurrency=4)
    by_name = {o.planned.name: o for o in batch.outcomes}
    assert [n for n, o in by_name.items() if o.succeeded] == ["ok", "explicit_ok"]
    assert {n: o.capacity for n, o in by_name.items() if o.capacity} == {
        "cube_full": CapacityResource.CUBE,
        "db_full": CapacityResource.DATABASE,
    }
    assert by_name["bug"].exception is bug
    assert by_name["bug"].capacity is None
    invalid = by_name["semantic_query_7"]
    assert invalid.executed is None and invalid.result is None and not invalid.succeeded
    assert batch.capacity == CapacityResource.CUBE


@pytest.mark.asyncio
async def test_cancelling_the_batch_cancels_in_flight_queries(monkeypatch):
    started = asyncio.Event()
    cancelled = []

    async def run(_workspace, query, **kwargs):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(query["measures"][0])
            raise

    monkeypatch.setattr("apps.artifacts.services.query_batch.run_semantic_query", run)
    plan = plan_artifact_queries(
        _artifact(None, [{"name": n, "measures": [n]} for n in ("a", "b")])
    )
    task = asyncio.create_task(
        execute_artifact_plan(plan, WORKSPACE, user_id="", row_limit=None, concurrency=2)
    )
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_raise_errors_propagates_the_first_error_and_starts_nothing_more(
    monkeypatch, concurrency
):
    bug = RuntimeError("bug")
    started = []

    cancelled = []

    async def run(_workspace, query, **kwargs):
        started.append(query["measures"][0])
        if query["measures"][0] == "q0":
            await asyncio.sleep(0)
            raise bug
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(query["measures"][0])
            raise

    monkeypatch.setattr("apps.artifacts.services.query_batch.run_semantic_query", run)
    plan = plan_artifact_queries(
        _artifact(None, [{"name": f"q{i}", "measures": [f"q{i}"]} for i in range(5)])
    )
    with pytest.raises(RuntimeError) as raised:
        await execute_artifact_plan(
            plan,
            WORKSPACE,
            user_id="",
            row_limit=None,
            concurrency=concurrency,
            raise_errors=True,
        )
    assert raised.value is bug
    # Siblings are cancelled and awaited before the error propagates.
    assert started == [f"q{i}" for i in range(concurrency)]
    assert cancelled == started[1:]
