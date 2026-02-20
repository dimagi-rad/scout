"""
Knowledge Retriever service for Scout data agent platform.

Assembles knowledge context from multiple sources into a formatted markdown
string suitable for inclusion in the agent's system prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.knowledge.models import AgentLearning, KnowledgeEntry, TableKnowledge

if TYPE_CHECKING:
    from apps.projects.models import TenantWorkspace


class KnowledgeRetriever:
    """
    Retrieves and formats knowledge context for an agent's system prompt.

    Aggregates knowledge from:
    - Knowledge entries (general-purpose: metrics, rules, queries, etc.)
    - Table knowledge (enriched metadata beyond the data dictionary)
    - Agent learnings (corrections discovered through trial and error)
    """

    MAX_AGENT_LEARNINGS = 20

    def __init__(self, workspace: TenantWorkspace) -> None:
        self.workspace = workspace

    def retrieve(self, user_question: str = "") -> str:
        """Retrieve and format all relevant knowledge as markdown."""
        sections: list[str] = []

        entries_section = self._format_knowledge_entries()
        if entries_section:
            sections.append(entries_section)

        tables_section = self._format_table_knowledge()
        if tables_section:
            sections.append(tables_section)

        learnings_section = self._format_agent_learnings()
        if learnings_section:
            sections.append(learnings_section)

        return "\n\n".join(sections)

    def _format_knowledge_entries(self) -> str:
        """Format knowledge entries as markdown sections."""
        entries = KnowledgeEntry.objects.filter(workspace=self.workspace).order_by("title")

        if not entries.exists():
            return ""

        lines: list[str] = ["## Knowledge Base", ""]

        for entry in entries:
            lines.append(f"### {entry.title}")
            lines.append("")
            lines.append(entry.content)
            lines.append("")

        return "\n".join(lines).rstrip()

    def _format_table_knowledge(self) -> str:
        """Format table knowledge with column notes and data quality notes."""
        tables = TableKnowledge.objects.filter(workspace=self.workspace).order_by("table_name")

        if not tables.exists():
            return ""

        lines: list[str] = ["## Table Context (beyond schema)", ""]

        for table in tables:
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

    def _format_agent_learnings(self) -> str:
        """Format active agent learnings as a bullet list."""
        learnings = AgentLearning.objects.filter(
            workspace=self.workspace,
            is_active=True,
        ).order_by("-confidence_score", "-times_applied")[: self.MAX_AGENT_LEARNINGS]

        if not learnings.exists():
            return ""

        lines: list[str] = ["## Learned Corrections", ""]

        for learning in learnings:
            lines.append(f"- {learning.description}")

            if learning.applies_to_tables:
                tables_str = ", ".join(f"`{t}`" for t in learning.applies_to_tables)
                lines.append(f"  - *Tables: {tables_str}*")

            if learning.confidence_score >= 0.8:
                lines.append(
                    f"  - *Confidence: {learning.confidence_score:.0%} "
                    f"(applied {learning.times_applied} times)*"
                )

        return "\n".join(lines)
