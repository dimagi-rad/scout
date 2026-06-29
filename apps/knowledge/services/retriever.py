"""
Knowledge Retriever service for Scout data agent platform.

Assembles knowledge context from multiple sources into a formatted markdown
string suitable for inclusion in the agent's system prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.knowledge.models import AgentLearning, KnowledgeEntry, TableKnowledge

if TYPE_CHECKING:
    from apps.workspaces.models import Workspace


# Char budget for the knowledge context injected into the system prompt, which
# is re-billed on every LLM call (arch #254, finding 01#4). Mirrors the graph's
# schema budget; bounding it keeps the cacheable prompt prefix small and stable.
KNOWLEDGE_CONTEXT_CHAR_BUDGET = 6000

_TRUNCATION_NOTICE = (
    "\n\n*(Knowledge context truncated to fit the prompt budget — "
    "open the Knowledge page to see the full set.)*"
)


class KnowledgeRetriever:
    """
    Retrieves and formats knowledge context for an agent's system prompt.

    Aggregates knowledge from:
    - Knowledge entries (general-purpose: metrics, rules, queries, etc.)
    - Table knowledge (enriched metadata beyond the data dictionary)
    - Agent learnings (corrections discovered through trial and error)
    """

    MAX_AGENT_LEARNINGS = 20

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def retrieve(self, user_question: str = "") -> str:
        """Retrieve and format all relevant knowledge as markdown.

        Output is bounded by ``KNOWLEDGE_CONTEXT_CHAR_BUDGET`` (arch #254, 01#4).
        ``user_question`` is accepted for API compatibility only; with no
        relevance index, entries are included in stable order until the budget
        is exhausted.
        """
        sections: list[str] = []

        entries_section = await self._format_knowledge_entries()
        if entries_section:
            sections.append(entries_section)

        tables_section = await self._format_table_knowledge()
        if tables_section:
            sections.append(tables_section)

        learnings_section = await self._format_agent_learnings()
        if learnings_section:
            sections.append(learnings_section)

        combined = "\n\n".join(sections)
        if len(combined) > KNOWLEDGE_CONTEXT_CHAR_BUDGET:
            keep = KNOWLEDGE_CONTEXT_CHAR_BUDGET - len(_TRUNCATION_NOTICE)
            combined = combined[: max(0, keep)].rstrip() + _TRUNCATION_NOTICE
        return combined

    async def _format_knowledge_entries(self) -> str:
        """Format knowledge entries as markdown sections."""
        entries = KnowledgeEntry.objects.filter(workspace=self.workspace).order_by("title")

        if not await entries.aexists():
            return ""

        lines: list[str] = ["## Knowledge Base", ""]

        async for entry in entries:
            lines.append(f"### {entry.title}")
            lines.append("")
            lines.append(entry.content)
            lines.append("")

        return "\n".join(lines).rstrip()

    async def _format_table_knowledge(self) -> str:
        """Format table knowledge with column notes and data quality notes."""
        tables = TableKnowledge.objects.filter(workspace=self.workspace).order_by("table_name")

        if not await tables.aexists():
            return ""

        lines: list[str] = ["## Table Context (beyond schema)", ""]

        async for table in tables:
            lines.append(f"### {table.table_name}")
            lines.append("")
            lines.append(table.description)
            lines.append("")

            if table.column_notes:
                lines.append("**Column Notes:**")
                for column, note in table.column_notes.items():
                    lines.append(f"- `{column}`: {note}")
                lines.append("")

            if table.data_quality_notes:
                lines.append("**Data Quality Notes:**")
                for note in table.data_quality_notes:
                    lines.append(f"- {note}")
                lines.append("")

            if table.related_tables:
                lines.append("**Related Tables:**")
                for relation in table.related_tables:
                    if isinstance(relation, dict):
                        related_table = relation.get("table", "")
                        join_hint = relation.get("join_hint", "")
                        if join_hint:
                            lines.append(f"- `{related_table}`: `{join_hint}`")
                        else:
                            lines.append(f"- `{related_table}`")
                    else:
                        lines.append(f"- `{relation}`")
                lines.append("")

            if table.refresh_frequency:
                lines.append(f"**Refresh Frequency:** {table.refresh_frequency}")
                lines.append("")

        return "\n".join(lines).rstrip()

    async def _format_agent_learnings(self) -> str:
        """Format active agent learnings as a bullet list."""
        learnings = AgentLearning.objects.filter(
            workspace=self.workspace,
            is_active=True,
        ).order_by("-confidence_score", "-times_applied")[: self.MAX_AGENT_LEARNINGS]

        if not await learnings.aexists():
            return ""

        lines: list[str] = ["## Learned Corrections", ""]

        async for learning in learnings:
            lines.append(f"- {learning.description}")

            if learning.applies_to_tables:
                tables_str = ", ".join(f"`{t}`" for t in learning.applies_to_tables)
                lines.append(f"  - *Tables: {tables_str}*")

            if learning.confidence_score >= 0.8:
                # times_applied effectively never increments today (arch #262,
                # finding 05#9), so only show a count when it's actually nonzero.
                if learning.times_applied > 0:
                    lines.append(
                        f"  - *Confidence: {learning.confidence_score:.0%} "
                        f"(applied {learning.times_applied} times)*"
                    )
                else:
                    lines.append(f"  - *Confidence: {learning.confidence_score:.0%}*")

        return "\n".join(lines)
