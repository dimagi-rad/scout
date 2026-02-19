"""
Learning tool for the Scout data agent platform.

This module provides a factory function to create a tool that allows the agent
to save discovered corrections and patterns as AgentLearning records. These
learnings are automatically injected into future prompts via the KnowledgeRetriever,
enabling the agent to improve over time without retraining.

This implements the "GPU-poor continuous learning" pattern described in the
architecture - the agent learns from its mistakes and shares that knowledge
across all future conversations.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

if TYPE_CHECKING:
    from apps.projects.models import Project
    from apps.users.models import User

logger = logging.getLogger(__name__)


# Valid categories for agent learnings
VALID_CATEGORIES = frozenset({
    "type_mismatch",
    "filter_required",
    "join_pattern",
    "aggregation",
    "naming",
    "data_quality",
    "business_logic",
    "other",
})


def create_save_learning_tool(project: Project, user: User):
    """
    Create a tool for saving agent learnings.

    The returned tool allows the agent to persist discovered corrections
    and patterns that will be automatically applied to future queries.
    This is the key mechanism for the agent's self-improvement capability.

    Learnings are stored with:
    - A plain English description (injected into future prompts)
    - The category of learning (for organization and retrieval)
    - The tables it applies to (for relevance filtering)
    - Original and corrected SQL (for reference and validation)

    Args:
        project: The Project model instance for scoping the learning.
        user: The User model instance who triggered the conversation
              where the learning was discovered.

    Returns:
        A LangChain tool function that saves learnings.

    Example:
        >>> tool = create_save_learning_tool(project, user)
        >>> result = tool.invoke({
        ...     "description": "The events.timestamp column stores epoch ms, not a timestamp",
        ...     "category": "type_mismatch",
        ...     "tables": ["events"],
        ... })
    """

    @tool
    def save_learning(
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
        # Import here to avoid circular imports
        from apps.knowledge.models import AgentLearning

        # Validate inputs
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

        # Validate tables exist in the project's data dictionary
        dd = project.data_dictionary or {}
        known_tables = set(dd.get("tables", {}).keys())

        if known_tables:
            unknown_tables = [t for t in tables if t not in known_tables]
            if unknown_tables:
                logger.warning(
                    "Learning references unknown tables: %s (known: %s)",
                    unknown_tables,
                    list(known_tables)[:5],
                )
                # Don't fail - the table might be valid but not in the cached dictionary

        # Check for duplicate learnings (same description for same tables)
        existing = AgentLearning.objects.filter(
            project=project,
            is_active=True,
            description__iexact=description.strip(),
        ).first()

        if existing:
            # Update the existing learning instead of creating a duplicate
            existing.confidence_score = min(1.0, existing.confidence_score + 0.1)
            existing.times_applied += 1
            existing.save(update_fields=["confidence_score", "times_applied"])

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

        # Create the new learning
        try:
            learning = AgentLearning.objects.create(
                project=project,
                description=description.strip(),
                category=category,
                applies_to_tables=tables,
                original_error="",  # Set from correction_context if available
                original_sql=original_sql,
                corrected_sql=corrected_sql,
                confidence_score=0.5,  # Start at neutral confidence
                times_applied=0,
                is_active=True,
                discovered_by_user=user,
            )

            logger.info(
                "Created new learning %s for project %s: %s",
                learning.id,
                project.slug,
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
            logger.exception(
                "Failed to save learning for project %s: %s",
                project.slug,
                str(e),
            )
            return {
                "status": "error",
                "message": f"Failed to save learning: {str(e)}",
                "learning_id": None,
                "tables_affected": [],
            }

    # Set the tool name explicitly
    save_learning.name = "save_learning"

    return save_learning


__all__ = [
    "create_save_learning_tool",
    "VALID_CATEGORIES",
]
