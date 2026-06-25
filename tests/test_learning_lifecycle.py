"""Tests for the learning lifecycle correctness fixes (arch #262, finding 05#9).

Covers:
- The retriever must not render '(applied N times)' usage claims when a
  learning has never actually been applied (times_applied == 0).
- save_learning's table validation must use a LIVE table source (TableKnowledge
  logical names), not the dead Workspace.data_dictionary field, so the warning
  path is actually reachable.
"""

import logging

import pytest

from apps.agents.tools.learning_tool import create_save_learning_tool
from apps.knowledge.models import AgentLearning, TableKnowledge
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


# ── save_learning: live table validation ─────────────────────────────────────


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_save_learning_warns_on_unknown_table_using_live_source(workspace, user, caplog):
    """save_learning's unknown-table validation must consult a LIVE table source
    (TableKnowledge logical names), not the dead Workspace.data_dictionary field.

    With a known table present, a learning referencing an unknown table should
    trigger the warning path (but still succeed — the warning is soft)."""
    await TableKnowledge.objects.acreate(
        workspace=workspace,
        table_name="cases",
        description="Case records",
    )

    tool = create_save_learning_tool(workspace, user)

    with caplog.at_level(logging.WARNING, logger="apps.agents.tools.learning_tool"):
        result = await tool.ainvoke(
            {
                "description": "The nonexistent table needs a special join pattern here.",
                "category": "join_pattern",
                "tables": ["nonexistent_table"],
            }
        )

    assert result["status"] == "saved"
    assert any(
        "unknown table" in rec.message.lower() or "nonexistent_table" in rec.message
        for rec in caplog.records
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_save_learning_no_warning_for_known_table(workspace, user, caplog):
    await TableKnowledge.objects.acreate(
        workspace=workspace,
        table_name="cases",
        description="Case records",
    )

    tool = create_save_learning_tool(workspace, user)

    with caplog.at_level(logging.WARNING, logger="apps.agents.tools.learning_tool"):
        result = await tool.ainvoke(
            {
                "description": "Cases table uses a soft-delete flag; filter is_deleted = false.",
                "category": "filter_required",
                "tables": ["cases"],
            }
        )

    assert result["status"] == "saved"
    assert not any("unknown table" in rec.message.lower() for rec in caplog.records)
