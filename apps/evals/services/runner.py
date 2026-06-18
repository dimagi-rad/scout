"""
Eval runner: answer a GoldenQuery via both the free-SQL path and the Cube
semantic-layer path, judge result equivalence, and persist EvalRun records.

Public API
----------
    runs = await run_eval(
        golden_query,
        runs=3,
        use_preagg=False,
        free_path=my_free_path,   # optional; inject for tests
        cube_path=my_cube_path,   # optional; inject for tests
        judge=my_judge,           # optional; inject for tests
    )
    # returns: list[EvalRun]  (length == runs)

Injectable interfaces
---------------------
free_path(question: str) -> dict
    Must return: {"sql": str, "result": <json-able>, "ms": int}
    Async callable.

cube_path(question: str) -> dict
    Must return: {"query": str, "result": <json-able>, "ms": int}
    Async callable.

judge(question: str, free_result, cube_result) -> dict
    Must return: {"match": bool, "confidence": float, "equivalence": str}
    Async callable.

Default paths
-------------
The default ``free_path`` and ``cube_path`` require a live Scout stack
(LangGraph agent, Cube SQL API) and are NOT suitable for unit tests.  They
raise ``NotImplementedError`` with a clear message so misconfigured test runs
fail loudly rather than silently skipping.

Inject real async callables via the keyword arguments when running against
the live stack (see the ``run_eval_command`` management command, Task 12).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from apps.evals.models import EvalRun, GoldenQuery
from apps.evals.services.judge import judge_equivalence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default path stubs (live-stack only — NOT for unit tests)
# ---------------------------------------------------------------------------


async def _default_free_path(question: str) -> dict:  # pragma: no cover
    """Default free-SQL path — requires live LangGraph agent.

    Raises:
        NotImplementedError: Always.  Inject a real free_path callable when
            running against the live stack (Task 12 management command).
    """
    raise NotImplementedError(
        "Default free_path requires the live Scout stack (LangGraph agent + DB). "
        "Inject a free_path callable, or use the run_eval management command. "
        "See M4 acceptance / Task 12."
    )


async def _default_cube_path(question: str) -> dict:  # pragma: no cover
    """Default Cube semantic-layer path — requires live Cube SQL API.

    Raises:
        NotImplementedError: Always.  Inject a real cube_path callable when
            running against the live stack (Task 12 management command).
    """
    raise NotImplementedError(
        "Default cube_path requires the live Scout stack (Cube SQL API + JWT). "
        "Inject a cube_path callable, or use the run_eval management command. "
        "See M4 acceptance / Task 12."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_eval(
    golden_query: GoldenQuery,
    *,
    runs: int = 3,
    use_preagg: bool = False,
    free_path: Callable[[str], Any] | None = None,
    cube_path: Callable[[str], Any] | None = None,
    judge: Callable[[str, Any, Any], Any] | None = None,
) -> list[EvalRun]:
    """Execute both answer paths for ``golden_query``, judge them, and persist EvalRuns.

    For each of ``runs`` iterations:
    1. Call ``free_path(question)`` → {"sql", "result", "ms"}.
    2. Call ``cube_path(question)`` → {"query", "result", "ms"}.
    3. Call ``judge(question, free_result, cube_result)`` → {"match", "confidence",
       "equivalence"}.
    4. Persist one ``EvalRun`` via async ORM ``acreate``.

    Args:
        golden_query: The ``GoldenQuery`` instance to evaluate.
        runs: Number of independent iterations to execute (default 3).
        use_preagg: Whether the Cube path is expected to use pre-aggregations.
            Stored verbatim on each ``EvalRun.used_preaggregation``.
        free_path: Async callable ``(question: str) -> dict``.  Defaults to a
            stub that raises ``NotImplementedError`` — inject for both tests
            and live runs.
        cube_path: Async callable ``(question: str) -> dict``.  Same default
            behaviour as ``free_path``.
        judge: Async callable ``(question, free_result, cube_result) -> dict``.
            Defaults to ``apps.evals.services.judge.judge_equivalence``.

    Returns:
        List of ``EvalRun`` instances (length == ``runs``), in creation order.
    """
    _free_path = free_path if free_path is not None else _default_free_path
    _cube_path = cube_path if cube_path is not None else _default_cube_path
    _judge = judge if judge is not None else judge_equivalence

    question = golden_query.question
    workspace = golden_query.workspace
    eval_runs: list[EvalRun] = []

    for i in range(runs):
        logger.debug(
            "run_eval iteration %d/%d for GoldenQuery %s", i + 1, runs, golden_query.pk
        )

        # ------------------------------------------------------------------
        # Free-SQL path
        # ------------------------------------------------------------------
        t0 = time.monotonic()
        free_out = await _free_path(question)
        # Allow the callable to supply its own ms; fall back to wall-clock.
        free_ms = int(free_out.get("ms", (time.monotonic() - t0) * 1000))
        free_sql = free_out.get("sql", "")
        free_result = free_out.get("result")

        # ------------------------------------------------------------------
        # Cube path
        # ------------------------------------------------------------------
        t1 = time.monotonic()
        cube_out = await _cube_path(question)
        cube_ms = int(cube_out.get("ms", (time.monotonic() - t1) * 1000))
        cube_query = cube_out.get("query", "")
        cube_result = cube_out.get("result")

        # ------------------------------------------------------------------
        # Judge equivalence
        # ------------------------------------------------------------------
        verdict = await _judge(question, free_result, cube_result)
        result_match = bool(verdict.get("match", False))
        match_confidence = float(verdict.get("confidence", 0.0))
        semantic_equivalence = str(verdict.get("equivalence", "failed"))

        # ------------------------------------------------------------------
        # Persist
        # ------------------------------------------------------------------
        run = await EvalRun.objects.acreate(
            workspace=workspace,
            golden_query=golden_query,
            free_sql=free_sql,
            free_sql_result=free_result,
            free_sql_ms=free_ms,
            cube_query=cube_query,
            cube_result=cube_result,
            cube_ms=cube_ms,
            result_match=result_match,
            match_confidence=match_confidence,
            semantic_equivalence=semantic_equivalence,
            used_preaggregation=use_preagg,
        )
        eval_runs.append(run)

        logger.info(
            "EvalRun %s: match=%s equivalence=%s confidence=%.2f",
            run.pk,
            result_match,
            semantic_equivalence,
            match_confidence,
        )

    return eval_runs
