"""
Fallback logging for the governed semantic layer.

When the agent cannot answer a metric-style question via the Cube semantic layer
and falls back to raw SQL, this service records a ``ModelGapSignal`` so the M5
self-improving loop can identify gaps in the semantic model.

Persistence primitive for model-gap signals.  The DETECTION/triggering of
fallbacks (calling this when the agent uses raw ``query`` for a metric question)
is wired in M5 (self-improving loop); this function is intentionally not yet
called from the graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.knowledge.models import ModelGapSignal

if TYPE_CHECKING:
    from apps.workspaces.models import Workspace


async def record_fallback(
    workspace: Workspace,
    question: str,
    sql: str = "",
) -> ModelGapSignal:
    """Persist a model-gap signal for a question that fell back to raw SQL.

    Args:
        workspace: The workspace in which the question was asked.
        question: The user's original question that could not be answered via
            the governed semantic layer.
        sql: The raw SQL the agent executed as a fallback. May be empty if the
            SQL is not available at the call site.

    Returns:
        The newly created ``ModelGapSignal`` instance.
    """
    return await ModelGapSignal.objects.acreate(
        workspace=workspace,
        question=question,
        sql=sql,
    )
