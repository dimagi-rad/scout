"""Knowledge context budget + single-heading tests (arch #254, finding 01#4).

The retriever previously concatenated ALL KnowledgeEntry + ALL TableKnowledge
rows into the system prompt with no size cap (only learnings were capped). With
a bulk-import endpoint the prompt could grow arbitrarily and was re-billed on
every LLM call. It also emitted its own ``## Knowledge Base`` heading which
``base.py`` then wrapped in a *second* ``## Knowledge Base`` heading.
"""

import pytest

from apps.knowledge.models import KnowledgeEntry, TableKnowledge
from apps.knowledge.services.retriever import KNOWLEDGE_CONTEXT_CHAR_BUDGET, KnowledgeRetriever


@pytest.mark.django_db(transaction=True)
class TestKnowledgeBudget:
    @pytest.mark.asyncio
    async def test_knowledge_section_respects_byte_budget(self, workspace, user):
        # Create knowledge whose total content vastly exceeds the budget.
        big = "X" * 2000
        for i in range(50):
            await KnowledgeEntry.objects.acreate(
                workspace=workspace,
                title=f"Entry {i}",
                content=big,
                tags=["test"],
                created_by=user,
            )

        retriever = KnowledgeRetriever(workspace)
        result = await retriever.retrieve()

        # The rendered knowledge context must be bounded (with a small allowance
        # for the truncation notice).
        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET + 200

    @pytest.mark.asyncio
    async def test_table_knowledge_counts_against_budget(self, workspace, user):
        big = "Y" * 2000
        for i in range(50):
            await TableKnowledge.objects.acreate(
                workspace=workspace,
                table_name=f"table_{i}",
                description=big,
                updated_by=user,
            )
        retriever = KnowledgeRetriever(workspace)
        result = await retriever.retrieve()
        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET + 200

    @pytest.mark.asyncio
    async def test_small_knowledge_not_truncated(self, workspace, user):
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="MRR",
            content="Monthly Recurring Revenue",
            tags=["metric"],
            created_by=user,
        )
        retriever = KnowledgeRetriever(workspace)
        result = await retriever.retrieve()
        assert "MRR" in result
        assert "Monthly Recurring Revenue" in result

    @pytest.mark.asyncio
    async def test_single_knowledge_base_heading(self, workspace, user):
        """No duplicated nested '## Knowledge Base' heading (01#4)."""
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="MRR",
            content="Monthly Recurring Revenue",
            tags=["metric"],
            created_by=user,
        )
        retriever = KnowledgeRetriever(workspace)
        result = await retriever.retrieve()
        # Exactly one Knowledge Base heading in the retriever output.
        assert result.count("## Knowledge Base") == 1
