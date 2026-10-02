"""Knowledge context budget + single-heading tests (arch #254, finding 01#4).

The retriever previously concatenated ALL KnowledgeEntry + ALL TableKnowledge
rows into the system prompt with no size cap (only learnings were capped). With
a bulk-import endpoint the prompt could grow arbitrarily and was re-billed on
every LLM call. It also emitted its own ``## Knowledge Base`` heading which
``base.py`` then wrapped in a *second* ``## Knowledge Base`` heading.
"""

import pytest

from apps.knowledge.models import AgentLearning, KnowledgeEntry, TableKnowledge
from apps.knowledge.services.retriever import (
    KNOWLEDGE_CONTEXT_CHAR_BUDGET,
    MAX_COLUMN_NOTES_PER_TABLE,
    KnowledgeRetriever,
    _fit_section,
)


def _stg_visits_notes(count: int) -> dict[str, str]:
    return {
        f"question_{i}": f"Question label number {i} for the deliver form — select_one; "
        "values: yes, no, unsure"
        for i in range(count)
    }


async def _add_learnings(workspace, user, count: int) -> list[str]:
    descriptions = []
    for i in range(count):
        description = f"Learning {i}: visit_date in stg_visits is UTC, convert before grouping."
        await AgentLearning.objects.acreate(
            workspace=workspace,
            description=description,
            category="type_mismatch",
            applies_to_tables=["stg_visits"],
            confidence_score=0.9,
            is_active=True,
            discovered_by_user=user,
        )
        descriptions.append(description)
    return descriptions


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


@pytest.mark.django_db(transaction=True)
class TestLearningsNotCrowdedOut:
    """#264: Connect's ~549-note stg_visits TableKnowledge pushed learnings out."""

    @pytest.mark.asyncio
    async def test_huge_stg_visits_keeps_learnings_within_budget(self, workspace, user):
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="stg_visits",
            description="Typed, labeled deliver-app form visits.",
            column_notes=_stg_visits_notes(549),
            updated_by=user,
        )
        descriptions = await _add_learnings(workspace, user, 20)

        result = await KnowledgeRetriever(workspace).retrieve()

        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
        assert "## Learned Corrections" in result
        for description in descriptions:
            assert description in result
        assert "### stg_visits" in result

    @pytest.mark.asyncio
    async def test_learnings_survive_entries_and_tables_over_budget(self, workspace, user):
        for i in range(30):
            await KnowledgeEntry.objects.acreate(
                workspace=workspace, title=f"Entry {i}", content="E" * 500, created_by=user
            )
        for i in range(10):
            await TableKnowledge.objects.acreate(
                workspace=workspace,
                table_name=f"stg_visits_{i}",
                description="T" * 500,
                column_notes=_stg_visits_notes(549),
                updated_by=user,
            )
        descriptions = await _add_learnings(workspace, user, 5)

        result = await KnowledgeRetriever(workspace).retrieve()

        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
        for description in descriptions:
            assert description in result
        # Display order is unchanged: learnings still come last.
        assert result.index("## Knowledge Base") < result.index("## Learned Corrections")

    @pytest.mark.asyncio
    async def test_truncation_never_cuts_mid_line(self, workspace, user):
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="stg_visits",
            description="Visits.",
            column_notes=_stg_visits_notes(549),
            updated_by=user,
        )
        for i in range(30):
            await KnowledgeEntry.objects.acreate(
                workspace=workspace,
                title=f"Entry {i}",
                content=" ".join(f"word{j}" for j in range(80)),
                created_by=user,
            )

        retriever = KnowledgeRetriever(workspace)
        result = await retriever.retrieve()
        full_lines = set(
            (await retriever._format_knowledge_entries()).splitlines()
            + (await retriever._format_table_knowledge()).splitlines()
        )

        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
        body = result.split("\n\n*(Knowledge context truncated")[0]
        assert body != result
        assert all(line in full_lines for line in body.splitlines())

    @pytest.mark.asyncio
    async def test_column_notes_capped_with_pointer(self, workspace, user):
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="stg_visits",
            description="Visits.",
            column_notes=_stg_visits_notes(549),
            updated_by=user,
        )

        tables = await KnowledgeRetriever(workspace)._format_table_knowledge()

        assert tables.count("- `question_") == MAX_COLUMN_NOTES_PER_TABLE
        omitted = 549 - MAX_COLUMN_NOTES_PER_TABLE
        assert f"{omitted} more column notes omitted" in tables
        assert "`describe_table`" in tables


class TestFitSection:
    def test_fits_unchanged(self):
        assert _fit_section("## A\n\n- one", 100) == "## A\n\n- one"

    def test_cuts_at_line_boundary_and_drops_dangling_headings(self):
        text = "## A\n\n- one\n- two\n\n### Sub\n\n- three is long"
        assert _fit_section(text, len(text) - 3) == "## A\n\n- one\n- two"

    def test_heading_only_section_dropped(self):
        assert _fit_section("## A\n\n- a long bullet line", 8) == ""

    def test_non_positive_limit(self):
        assert _fit_section("## A\n\n- one", 0) == ""
