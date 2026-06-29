"""
Learning tool for the Scout data agent platform.

Factory for a tool that saves discovered corrections as AgentLearning records,
which KnowledgeRetriever injects into future prompts so the agent improves
without retraining (the "GPU-poor continuous learning" pattern).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

from apps.knowledge.models import AgentLearning, TableKnowledge

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)


VALID_CATEGORIES = frozenset(
    {
        "type_mismatch",
        "filter_required",
        "join_pattern",
        "aggregation",
        "naming",
        "data_quality",
        "business_logic",
        "other",
    }
)


def create_save_learning_tool(workspace: Workspace, user: User):
    """Create the save_learning tool scoped to a workspace and discovering user."""

    @tool
    async def save_learning(
        description: str,
        category: str,
        tables: list[str],
        original_sql: str = "",
        corrected_sql: str = "",
    ) -> dict[str, Any]:
        """
        Save a learned correction for future queries.

        Call this tool AFTER you have successfully corrected a query error.
        The learning will be automatically applied to future queries,
        preventing the same mistake from happening again.

        Guidelines for good learnings:
        - Be specific and actionable
        - Include the exact fix, not just what was wrong
        - Reference specific column/table names
        - Explain WHY the fix works

        Good example:
        "The events.timestamp column stores Unix epoch milliseconds (not seconds).
        Use to_timestamp(timestamp / 1000.0) to convert to a PostgreSQL timestamp."

        Bad example:
        "The timestamp column was wrong."

        Args:
            description: Clear, actionable description of what was learned.
                Must be detailed enough that another agent (or future you)
                can apply this learning correctly. Include:
                - What was the problem
                - What is the correct approach
                - Any specific syntax or patterns to use

            category: Classification of the learning. Must be one of:
                - type_mismatch: Column type different than expected
                - filter_required: Query needs a specific WHERE clause
                - join_pattern: Correct way to join specific tables
                - aggregation: Gotcha with aggregation/grouping
                - naming: Column or table naming convention
                - data_quality: Known data issues (NULLs, duplicates, etc.)
                - business_logic: Domain-specific rules
                - other: Anything that doesn't fit above

            tables: List of table names this learning applies to.
                Future queries involving these tables will see this learning.
                Use actual table names from the schema.

            original_sql: The SQL that failed (optional but recommended).
                Helps validate the learning and provides context.

            corrected_sql: The SQL that worked (optional but recommended).
                Shows the correct pattern to follow.

        Returns:
            A dict with:
            - learning_id: UUID of the created learning (as string)
            - status: "saved" on success, "error" on failure
            - message: Confirmation or error message
            - tables_affected: List of tables the learning applies to
        """
        if not description or len(description.strip()) < 20:
            return {
                "status": "error",
                "message": "Description is too short. Please provide a detailed, "
                "actionable description of at least 20 characters.",
                "learning_id": None,
                "tables_affected": [],
            }

        if category not in VALID_CATEGORIES:
            return {
                "status": "error",
                "message": f"Invalid category '{category}'. Must be one of: "
                f"{', '.join(sorted(VALID_CATEGORIES))}",
                "learning_id": None,
                "tables_affected": [],
            }

        if not tables:
            return {
                "status": "error",
                "message": "Please specify at least one table this learning applies to.",
                "learning_id": None,
                "tables_affected": [],
            }

        # Validate tables against a LIVE source: TableKnowledge's logical table
        # names. The old code read the never-written ``Workspace.data_dictionary``,
        # so this check was always skipped (arch #262, finding 05#9; TableKnowledge
        # keyed by logical name after 01#5). Soft warning only — never rejects, as
        # a table may be valid without an annotation row yet.
        known_tables = {
            name
            async for name in TableKnowledge.objects.filter(workspace=workspace).values_list(
                "table_name", flat=True
            )
        }

        if known_tables:
            unknown_tables = [t for t in tables if t not in known_tables]
            if unknown_tables:
                logger.warning(
                    "Learning references unknown tables: %s (known: %s)",
                    unknown_tables,
                    list(known_tables)[:5],
                )

        existing = await AgentLearning.objects.filter(
            workspace=workspace,
            is_active=True,
            description__iexact=description.strip(),
        ).afirst()

        if existing:
            existing.confidence_score = min(1.0, existing.confidence_score + 0.1)
            existing.times_applied += 1
            await existing.asave(update_fields=["confidence_score", "times_applied"])

            logger.info(
                "Updated existing learning %s (confidence: %.2f)",
                existing.id,
                existing.confidence_score,
            )

            return {
                "status": "updated",
                "message": f"This learning already exists. Increased confidence to "
                f"{existing.confidence_score:.0%}.",
                "learning_id": str(existing.id),
                "tables_affected": existing.applies_to_tables,
            }

        try:
            learning = await AgentLearning.objects.acreate(
                workspace=workspace,
                description=description.strip(),
                category=category,
                applies_to_tables=tables,
                original_error="",
                original_sql=original_sql,
                corrected_sql=corrected_sql,
                confidence_score=0.5,  # neutral
                times_applied=0,
                is_active=True,
                discovered_by_user=user,
            )

            logger.info(
                "Created new learning %s for workspace %s: %s",
                learning.id,
                workspace.id,
                description[:50] + "..." if len(description) > 50 else description,
            )

            return {
                "status": "saved",
                "message": f"Learning saved successfully. This correction will be "
                f"automatically applied to future queries involving: "
                f"{', '.join(tables)}.",
                "learning_id": str(learning.id),
                "tables_affected": tables,
            }

        except Exception as e:
            logger.exception("Failed to save learning for workspace %s", workspace.id)
            return {
                "status": "error",
                "message": f"Failed to save learning: {e!s}",
                "learning_id": None,
                "tables_affected": [],
            }

    save_learning.name = "save_learning"

    return save_learning


__all__ = [
    "VALID_CATEGORIES",
    "create_save_learning_tool",
]
