"""
Knowledge Retriever service for Scout data agent platform.

Assembles knowledge context from multiple sources into a formatted markdown
string suitable for inclusion in the agent's system prompt.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from apps.knowledge.models import AgentLearning, KnowledgeEntry, TableKnowledge

if TYPE_CHECKING:
    from apps.workspaces.models import Workspace

_SQL_FENCE_RE = re.compile(r"```sql\b.*?```", re.IGNORECASE | re.DOTALL)
_SQL_STATEMENT_LINE_RE = re.compile(
    r"(?im)^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b.*$"
)
_SQL_REDACTION = "[SQL example removed; use semantic_query members instead.]"


def _sanitize_prompt_content(value: str) -> str:
    """Remove legacy SQL snippets before knowledge is injected into prompts."""
    value = _SQL_FENCE_RE.sub(_SQL_REDACTION, value)
    return _SQL_STATEMENT_LINE_RE.sub(_SQL_REDACTION, value)


# Char budget for the knowledge context injected into the system prompt, which
# is re-billed on every LLM call (arch #254, finding 01#4). Mirrors the graph's
# schema budget; bounding it keeps the cacheable prompt prefix small and stable.
KNOWLEDGE_CONTEXT_CHAR_BUDGET = 6000

_TRUNCATION_NOTICE = (
    "\n\n*(Knowledge context truncated to fit the prompt budget — "
    "open the Knowledge page to see the full set.)*"
)

_SECTION_SEPARATOR = "\n\n"

# Applied only when the context is over budget: Connect's generated stg_visits
# TableKnowledge carries hundreds of column notes (30-50 KB rendered), and
# uncapped, one table spends the whole budget (#264).
MAX_COLUMN_NOTES_PER_TABLE = 40


def _without_dangling_headings(lines: list[str]) -> list[str]:
    """Drop trailing blanks, headings and labels; [] if no content line remains."""
    lines = list(lines)
    while lines and (
        not lines[-1].strip() or lines[-1].startswith("#") or lines[-1].endswith(":**")
    ):
        lines.pop()
    if all(not line.strip() or line.startswith("#") for line in lines):
        return []
    return lines


def _fit_section(text: str, limit: int) -> str:
    """Trim *text* to at most *limit* chars at a line boundary.

    Trailing headings and labels left without their content are dropped, and a
    section reduced to nothing but headings is dropped entirely.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    lines = _without_dangling_headings(text[: limit + 1].split("\n")[:-1])
    if lines:
        return "\n".join(lines)
    # Its first content line alone overruns the limit (e.g. a one-paragraph entry):
    # cut that line at a word rather than lose the whole section.
    head = text[: limit - 1]
    cut = head.rfind(" ")
    lines = _without_dangling_headings(head[:cut].split("\n")) if cut > 0 else []
    return "\n".join(lines) + "…" if lines else ""


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

        When over budget, sections claim space in priority order — learnings,
        then knowledge entries, then table context — so a bulky table dump
        cannot crowd out the short, high-value learnings (#264). Display order
        is unchanged, each table's column notes are capped, and each section is
        cut only at a line boundary.
        """
        sections = {
            "entries": await self._format_knowledge_entries(),
            "tables": await self._format_table_knowledge(),
            "learnings": await self._format_agent_learnings(),
        }
        combined = _SECTION_SEPARATOR.join(text for text in sections.values() if text)
        if len(combined) <= KNOWLEDGE_CONTEXT_CHAR_BUDGET:
            return combined

        sections["tables"] = await self._format_table_knowledge(
            max_column_notes=MAX_COLUMN_NOTES_PER_TABLE
        )
        remaining = KNOWLEDGE_CONTEXT_CHAR_BUDGET - len(_TRUNCATION_NOTICE)
        fitted: dict[str, str] = {}
        for name in ("learnings", "entries", "tables"):
            if not sections[name]:
                continue
            separator = len(_SECTION_SEPARATOR) if fitted else 0
            kept = _fit_section(sections[name], remaining - separator)
            if kept:
                fitted[name] = kept
                remaining -= len(kept) + separator
        kept_sections = [fitted[name] for name in sections if name in fitted]
        return _SECTION_SEPARATOR.join(kept_sections) + _TRUNCATION_NOTICE

    async def _format_knowledge_entries(self) -> str:
        """Format knowledge entries as markdown sections."""
        entries = KnowledgeEntry.objects.filter(workspace=self.workspace).order_by("title")

        if not await entries.aexists():
            return ""

        lines: list[str] = ["## Knowledge Base", ""]

        async for entry in entries:
            lines.append(f"### {entry.title}")
            lines.append("")
            lines.append(_sanitize_prompt_content(entry.content))
            lines.append("")

        return "\n".join(lines).rstrip()

    async def _format_table_knowledge(self, max_column_notes: int | None = None) -> str:
        """Format table knowledge with column notes and data quality notes."""
        tables = TableKnowledge.objects.filter(workspace=self.workspace).order_by("table_name")

        if not await tables.aexists():
            return ""

        lines: list[str] = ["## Table Context (beyond schema)", ""]

        async for table in tables:
            lines.append(f"### {table.table_name}")
            lines.append("")
            lines.append(_sanitize_prompt_content(table.description))
            lines.append("")

            if table.column_notes:
                lines.append("**Column Notes:**")
                # jsonb keeps no insertion order, so sort for a stated, stable cut.
                notes = sorted(table.column_notes.items())
                shown = notes if max_column_notes is None else notes[:max_column_notes]
                for column, note in shown:
                    lines.append(f"- `{column}`: {_sanitize_prompt_content(str(note))}")
                if len(notes) > len(shown):
                    lines.append(
                        f"- … notes for {len(notes) - len(shown)} more columns are left out "
                        f"to fit the prompt; `describe_table` gives only their names and types."
                    )
                lines.append("")

            if table.data_quality_notes:
                lines.append("**Data Quality Notes:**")
                for note in table.data_quality_notes:
                    lines.append(f"- {_sanitize_prompt_content(str(note))}")
                lines.append("")

            if table.related_tables:
                lines.append("**Related Tables:**")
                for relation in table.related_tables:
                    if isinstance(relation, dict):
                        related_table = relation.get("table", "")
                        join_hint = relation.get("join_hint", "")
                        if join_hint:
                            lines.append(
                                f"- `{related_table}`: `{_sanitize_prompt_content(str(join_hint))}`"
                            )
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
            lines.append(f"- {_sanitize_prompt_content(learning.description)}")

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
