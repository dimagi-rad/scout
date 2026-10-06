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
    _TRUNCATION_NOTICE,
    KNOWLEDGE_CONTEXT_CHAR_BUDGET,
    LEARNINGS_CHAR_CAP,
    MAX_COLUMN_NOTES_PER_TABLE,
    WORKSPACE_MEMORY_HEADING,
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
        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET

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
        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET

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
        assert WORKSPACE_MEMORY_HEADING in result
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
        assert result.index("## Knowledge Base") < result.index(WORKSPACE_MEMORY_HEADING)

    @pytest.mark.asyncio
    async def test_truncation_never_cuts_mid_line(self, workspace, user):
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="stg_visits",
            description="Visits.",
            column_notes=_stg_visits_notes(549),
            updated_by=user,
        )
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="stg_visits_2",
            description="More visits.",
            column_notes=_stg_visits_notes(549),
            updated_by=user,
        )
        await _add_learnings(workspace, user, 5)
        for i in range(3):
            await KnowledgeEntry.objects.acreate(
                workspace=workspace,
                title=f"Entry {i}",
                content=" ".join(f"word{j}" for j in range(80)),
                created_by=user,
            )

        retriever = KnowledgeRetriever(workspace)
        result = await retriever.retrieve()
        capped_tables = await retriever._format_table_knowledge(
            max_column_notes=MAX_COLUMN_NOTES_PER_TABLE
        )
        full_lines = set(
            (await retriever._format_knowledge_entries()).splitlines()
            + capped_tables.splitlines()
            + (await retriever._format_agent_learnings()).splitlines()
        )

        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
        body = result.split("\n\n*(Knowledge context truncated")[0]
        assert body != result
        assert "### stg_visits" in body
        assert "### stg_visits_2" not in body
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

        result = await KnowledgeRetriever(workspace).retrieve()

        assert result.count("- `question_") == MAX_COLUMN_NOTES_PER_TABLE
        assert f"notes for {549 - MAX_COLUMN_NOTES_PER_TABLE} more columns" in result
        assert "`describe_table`" in result

    @pytest.mark.asyncio
    async def test_column_notes_not_capped_under_budget(self, workspace, user):
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="small_table",
            description="Small.",
            column_notes={f"c{i}": "n" for i in range(MAX_COLUMN_NOTES_PER_TABLE + 10)},
            updated_by=user,
        )

        result = await KnowledgeRetriever(workspace).retrieve()

        assert result.count("- `c") == MAX_COLUMN_NOTES_PER_TABLE + 10
        assert "truncated" not in result

    @pytest.mark.asyncio
    async def test_section_filling_remaining_budget_exactly(self, workspace, user):
        """A later section must not slip through untrimmed when nothing is left."""
        learning_heading = WORKSPACE_MEMORY_HEADING + "\n\n- "
        await AgentLearning.objects.acreate(
            workspace=workspace,
            description="L" * (LEARNINGS_CHAR_CAP - len(learning_heading)),
            category="type_mismatch",
            confidence_score=0.5,
            is_active=True,
            discovered_by_user=user,
        )
        remaining = KNOWLEDGE_CONTEXT_CHAR_BUDGET - len(_TRUNCATION_NOTICE) - LEARNINGS_CHAR_CAP
        entry_heading = "## Knowledge Base\n\n### E\n\n"
        await KnowledgeEntry.objects.acreate(
            workspace=workspace,
            title="E",
            content="E" * (remaining - len("\n\n") - len(entry_heading)),
            created_by=user,
        )
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="stg_visits",
            description="Visits.",
            column_notes=_stg_visits_notes(549),
            updated_by=user,
        )

        result = await KnowledgeRetriever(workspace).retrieve()

        assert len(result) == KNOWLEDGE_CONTEXT_CHAR_BUDGET
        assert "## Table Context" not in result

    @pytest.mark.asyncio
    async def test_learnings_capped_so_entries_keep_space(self, workspace, user):
        for i in range(20):
            await AgentLearning.objects.acreate(
                workspace=workspace,
                description=f"Learning {i}: " + "x " * 200,
                category="type_mismatch",
                confidence_score=0.5,
                is_active=True,
                discovered_by_user=user,
            )
        await KnowledgeEntry.objects.acreate(
            workspace=workspace, title="MRR", content="Monthly Recurring Revenue", created_by=user
        )

        result = await KnowledgeRetriever(workspace).retrieve()

        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
        assert "Monthly Recurring Revenue" in result
        learnings = result[result.index(WORKSPACE_MEMORY_HEADING) :].split("\n\n*(")[0]
        # Entries leave most of the budget unused, so learnings get it back.
        assert len(learnings) > LEARNINGS_CHAR_CAP

    @pytest.mark.asyncio
    async def test_entries_not_evicted_when_learnings_compete(self, workspace, user):
        for i in range(20):
            await AgentLearning.objects.acreate(
                workspace=workspace,
                description=f"Learning {i}: " + "x " * 200,
                category="type_mismatch",
                confidence_score=0.5,
                is_active=True,
                discovered_by_user=user,
            )
        for i in range(30):
            await KnowledgeEntry.objects.acreate(
                workspace=workspace, title=f"Entry {i}", content="E" * 500, created_by=user
            )

        result = await KnowledgeRetriever(workspace).retrieve()

        assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
        assert "### Entry 0" in result
        assert WORKSPACE_MEMORY_HEADING in result

    @pytest.mark.asyncio
    async def test_no_truncation_notice_when_cap_alone_fits(self, workspace, user):
        await TableKnowledge.objects.acreate(
            workspace=workspace,
            table_name="stg_visits",
            description="Visits.",
            column_notes=_stg_visits_notes(100),
            updated_by=user,
        )

        result = await KnowledgeRetriever(workspace).retrieve()

        assert result.count("- `question_") == MAX_COLUMN_NOTES_PER_TABLE
        assert "truncated" not in result


class TestFitSection:
    def test_fits_unchanged(self):
        assert _fit_section("## A\n\n- one", 100) == "## A\n\n- one"

    def test_cuts_at_line_boundary_and_drops_dangling_headings(self):
        text = "## A\n\n- one\n- two\n\n### Sub\n\n- three is long"
        assert _fit_section(text, len(text) - 3) == "## A\n\n- one\n- two"

    def test_heading_only_section_dropped(self):
        assert _fit_section("## A\n\n- a long bullet line", 8) == ""

    def test_single_overlong_line_cut_at_word(self):
        text = "## A\n\n### Entry\n\n" + " ".join(f"w{i}" for i in range(100))
        fitted = _fit_section(text, 60)
        assert len(fitted) <= 60
        assert fitted.startswith("## A\n\n### Entry\n\nw0 w1")
        assert fitted.endswith("…")
        assert fitted[:-1].split()[-1] in {f"w{i}" for i in range(100)}

    def test_prefers_word_cut_when_hard_cut_qualifies(self):
        text = "## A\n\n### Entry\n\n" + " ".join(f"w{i}" for i in range(100))
        for limit in range(40, 60):
            fitted = _fit_section(text, limit)
            assert fitted.endswith("…")
            assert fitted[:-1].split()[-1] in {f"w{i}" for i in range(100)}
            assert len(fitted) <= limit

    def test_stub_word_cut_falls_back_to_hard_cut(self):
        fitted = _fit_section("## L\n\n- a " + "x" * 100, 40)
        assert fitted.startswith("## L\n\n- a xxx")
        assert fitted.endswith("x…")
        assert len(fitted) <= 40

    def test_overlong_line_without_spaces_hard_cut(self):
        fitted = _fit_section("## A\n\n### Blob\n\n" + "x" * 100, 50)
        assert len(fitted) <= 50
        assert fitted.startswith("## A\n\n### Blob\n\nxxx")
        assert fitted.endswith("…")

    @pytest.mark.parametrize("limit", [0, -1, -2, -50])
    def test_non_positive_limit(self, limit):
        assert _fit_section("## A\n\n- one\n- two\n- three", limit) == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_one_paragraph_entry_over_budget_keeps_a_prefix(workspace, user):
    await KnowledgeEntry.objects.acreate(
        workspace=workspace,
        title="Program context",
        content=" ".join(f"word{i}" for i in range(3000)),
        created_by=user,
    )

    result = await KnowledgeRetriever(workspace).retrieve()

    assert len(result) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET
    assert "### Program context" in result
    assert "word0 word1" in result
