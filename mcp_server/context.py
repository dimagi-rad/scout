"""Project context for the MCP server.

Holds the loaded Project instance so tool handlers can access
project configuration, database connection params, and query limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Module-level project context, set during server startup
_project_context: ProjectContext | None = None


@dataclass(frozen=True)
class ProjectContext:
    """Immutable snapshot of project configuration for tool handlers."""

    project_id: str
    project_name: str
    db_schema: str
    allowed_tables: list[str]
    excluded_tables: list[str]
    max_rows_per_query: int
    max_query_timeout_seconds: int
    readonly_role: str
    connection_params: dict[str, Any]

    @classmethod
    def from_project(cls, project: Any) -> ProjectContext:
        """Create a ProjectContext from a Django Project model instance."""
        return cls(
            project_id=str(project.id),
            project_name=project.name,
            db_schema=project.db_schema,
            allowed_tables=project.allowed_tables or [],
            excluded_tables=project.excluded_tables or [],
            max_rows_per_query=project.max_rows_per_query,
            max_query_timeout_seconds=project.max_query_timeout_seconds,
            readonly_role=project.readonly_role or "",
            connection_params=project.get_connection_params(),
        )


def set_project_context(ctx: ProjectContext) -> None:
    """Set the global project context. Called once during server startup."""
    global _project_context
    _project_context = ctx


def get_project_context() -> ProjectContext:
    """Get the global project context. Raises if not initialized."""
    if _project_context is None:
        raise RuntimeError(
            "Project context not initialized. "
            "Pass --project-id when starting the MCP server."
        )
    return _project_context
