"""Plan and execute an artifact's semantic queries as one batch.

View Data and graph runtime checks both run this; each keeps its own response
shape, row limit and policy (caching, HTTP status, key diagnostics).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from apps.common.capacity import CapacityResource, classify_capacity_error
from apps.semantic.services.query import is_capacity_exhausted, run_semantic_query
from apps.semantic.services.query_outcomes import QueryReadiness

from .query_context import resolve_artifact_queries


@dataclass(frozen=True)
class PlannedQuery:
    """One query as the artifact declares it; ``ArtifactQueryOutcome.executed`` has the limit."""

    name: str
    entry: Any

    @property
    def spec(self) -> dict | None:
        if not isinstance(self.entry, dict):
            return None
        return {key: value for key, value in self.entry.items() if key != "name"}


@dataclass(frozen=True)
class ArtifactQueryPlan:
    queries: tuple[PlannedQuery, ...]
    query_context: dict | None

    @property
    def entries(self) -> list:
        return [query.entry for query in self.queries]


@dataclass(frozen=True)
class ArtifactQueryOutcome:
    planned: PlannedQuery
    executed: dict | None = None
    result: dict | None = None
    exception: BaseException | None = None

    @property
    def capacity(self) -> CapacityResource | None:
        if self.exception is not None:
            exhausted = classify_capacity_error(self.exception)
            return exhausted.resource if exhausted is not None else None
        if self.result is not None and is_capacity_exhausted(self.result):
            return CapacityResource.CUBE
        return None

    @property
    def succeeded(self) -> bool:
        return bool(
            self.result is not None
            and self.result.get("success", True)
            and not self.result.get("error")
        )


@dataclass(frozen=True)
class ArtifactBatchResult:
    outcomes: tuple[ArtifactQueryOutcome, ...]

    @property
    def capacity(self) -> CapacityResource | None:
        return next((o.capacity for o in self.outcomes if o.capacity is not None), None)


def _plan(entries: list, query_context: dict | None) -> ArtifactQueryPlan:
    return ArtifactQueryPlan(
        queries=tuple(
            PlannedQuery(
                name=entry.get("name", f"semantic_query_{index}")
                if isinstance(entry, dict)
                else f"semantic_query_{index}",
                entry=entry,
            )
            for index, entry in enumerate(entries)
        ),
        query_context=query_context,
    )


def plan_story_queries(doc, runtime=None) -> ArtifactQueryPlan:
    """Resolve a graph document's bound queries; raises ``DateContextError``."""
    queries, context = resolve_artifact_queries(doc, runtime)
    return _plan(queries, context)


def plan_artifact_queries(artifact, runtime=None) -> ArtifactQueryPlan:
    """The queries View Data runs: graph bindings, else the stored ``semantic_queries``.

    A narrative-only document has no bound queries, so explicitly stored ones
    still run and no date context is claimed for them.
    """
    doc = (artifact.data or {}).get("story_doc")
    if isinstance(doc, dict) and doc.get("blocks"):
        plan = plan_story_queries(doc, runtime)
        if plan.queries:
            return plan
    return _plan(artifact.semantic_queries or [], None)


async def execute_artifact_plan(
    plan: ArtifactQueryPlan,
    workspace,
    *,
    user_id: str,
    row_limit: int | None,
    concurrency: int,
    raise_errors: bool = False,
) -> ArtifactBatchResult:
    """Run every planned query against ``workspace``, which the caller has authorized.

    Raised errors are captured per query so the caller decides whether one
    fails the batch, unless ``raise_errors``: then the first one propagates and
    queries not yet started never run. Readiness is inspected once per batch.
    """
    executed = []
    for planned in plan.queries:
        spec = planned.spec
        if spec is not None and row_limit is not None:
            spec.setdefault("limit", row_limit)
        executed.append(spec)
    readiness = QueryReadiness(workspace, [spec for spec in executed if spec is not None])
    slots = asyncio.Semaphore(concurrency)
    aborted = False

    async def run_one(planned: PlannedQuery, spec: dict | None) -> ArtifactQueryOutcome:
        nonlocal aborted
        if spec is None:
            return ArtifactQueryOutcome(planned)
        async with slots:
            if aborted:
                return ArtifactQueryOutcome(planned, spec)
            try:
                result = await run_semantic_query(
                    workspace, spec, user_id=user_id, readiness=readiness
                )
            except Exception as exc:
                if raise_errors:
                    aborted = True
                    raise
                return ArtifactQueryOutcome(planned, spec, exception=exc)
        return ArtifactQueryOutcome(planned, spec, result=result)

    tasks = [
        asyncio.ensure_future(run_one(planned, spec))
        for planned, spec in zip(plan.queries, executed, strict=True)
    ]
    try:
        outcomes = await asyncio.gather(*tasks)
    except BaseException:
        # gather leaves siblings running after the first raise; don't orphan them,
        # and retrieve their results so a second failure isn't logged as unretrieved.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return ArtifactBatchResult(tuple(outcomes))
