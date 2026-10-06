"""Tests for the learning lifecycle correctness fixes (arch #262, finding 05#9).

Covers:
- The retriever must not render '(applied N times)' usage claims when a
  learning has never actually been applied (times_applied == 0).
"""

import pytest

from apps.knowledge.models import AgentLearning
from apps.knowledge.services.retriever import KnowledgeRetriever

# ── retriever: no false usage claims ─────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_retriever_omits_applied_count_when_never_applied(workspace, user):
    """A high-confidence learning that has never been applied must not claim
    '(applied 0 times)' — that implies usage that never happened."""
    await AgentLearning.objects.acreate(
        workspace=workspace,
        description="Amount column is in cents; divide by 100.",
        category="type_mismatch",
        applies_to_tables=["orders"],
        confidence_score=0.9,
        times_applied=0,
        is_active=True,
        discovered_by_user=user,
    )

    result = await KnowledgeRetriever(workspace).retrieve()

    assert "cents" in result.lower()
    assert "applied" not in result.lower()
    assert "times" not in result.lower()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_retriever_reports_applied_count_when_actually_applied(workspace, user):
    """When a learning HAS been applied, the count may be shown."""
    await AgentLearning.objects.acreate(
        workspace=workspace,
        description="Amount column is in cents; divide by 100.",
        category="type_mismatch",
        applies_to_tables=["orders"],
        confidence_score=0.9,
        times_applied=3,
        is_active=True,
        discovered_by_user=user,
    )

    result = await KnowledgeRetriever(workspace).retrieve()

    assert "applied 3 times" in result.lower()
