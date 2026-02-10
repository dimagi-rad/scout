"""
Artifact creation tools for the Scout data agent platform.

This module provides factory functions to create tools that allow the agent
to generate interactive visualizations and content artifacts. Artifacts can be
React components, HTML, Markdown, Plotly charts, or SVG graphics.

The tools support:
- Creating new artifacts with code and optional data
- Updating existing artifacts (creates new versions preserving history)
- Linking artifacts to source SQL queries for provenance tracking
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

if TYPE_CHECKING:
    from apps.projects.models import Project
    from apps.users.models import User

logger = logging.getLogger(__name__)


# Valid artifact types that can be created
VALID_ARTIFACT_TYPES = frozenset({
    "react",
    "html",
    "markdown",
    "plotly",
    "svg",
})


def create_artifact_tools(
    project: "Project",
    user: "User | None",
    conversation_id: str | None = None
) -> list:
    """
    Factory function to create artifact creation tools for a specific project.

    Creates two tools:
    1. create_artifact: Create a new artifact with code and optional data
    2. update_artifact: Create a new version of an existing artifact

    Args:
        project: The Project model instance for scoping artifacts.
        user: The User model instance who triggered the conversation.
              Used to track artifact ownership.
        conversation_id: The conversation/thread ID for tracking artifact provenance.

    Returns:
        A list of LangChain tool functions [create_artifact, update_artifact].

    Example:
        >>> from apps.projects.models import Project
        >>> project = Project.objects.get(slug="analytics")
        >>> tools = create_artifact_tools(project, user, conversation_id="thread-123")
        >>> create_tool, update_tool = tools
    """

    @tool
    def create_artifact(
        title: str,
        artifact_type: str,
        code: str,
        description: str = "",
        data: dict | None = None,
        source_queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Create a new interactive artifact (visualization, chart, or content).

        Use this tool when the user needs a visual representation of data,
        such as charts, tables, dashboards, or formatted content. The artifact
        will be rendered in an interactive preview.

        Args:
            title: Human-readable title for the artifact. Should describe
                what the visualization shows (e.g., "Monthly Revenue Trend",
                "User Signup Funnel").

            artifact_type: Type of artifact to create. Must be one of:
                - "react": Interactive React component (recommended for dashboards,
                  complex visualizations). Use Recharts for charts.
                - "plotly": Plotly chart specification (good for statistical charts).
                  Pass the Plotly figure spec as the code.
                - "html": Static HTML content (for simple tables, formatted text).
                - "markdown": Markdown content (for documentation, reports).
                - "svg": SVG graphic (for custom diagrams, icons).

            code: The source code for the artifact:
                - For "react": JSX code with a default export component
                - For "plotly": JSON string of Plotly figure specification
                - For "html": HTML markup
                - For "markdown": Markdown text
                - For "svg": SVG markup

            description: Optional description of what this artifact visualizes.
                Helps users understand the artifact's purpose.

            data: Optional JSON data to pass to the artifact. For React components,
                this is available as a `data` prop. Useful for separating data
                from presentation logic.

            source_queries: Optional list of SQL queries that generated the data
                for this artifact. Aids provenance tracking.

        Returns:
            A dict containing:
            - artifact_id: UUID of the created artifact (as string)
            - status: "created" on success, "error" on failure
            - title: The artifact title
            - type: The artifact type
            - render_url: URL path to render the artifact
            - message: Success or error message

        Example:
            >>> create_artifact(
            ...     title="Monthly Active Users",
            ...     artifact_type="react",
            ...     code='''
            ...     import { LineChart, Line, XAxis, YAxis, Tooltip } from "recharts";
            ...     export default function Chart({ data }) {
            ...       return (
            ...         <LineChart width={600} height={300} data={data}>
            ...           <XAxis dataKey="month" />
            ...           <YAxis />
            ...           <Tooltip />
            ...           <Line type="monotone" dataKey="users" stroke="#8884d8" />
            ...         </LineChart>
            ...       );
            ...     }
            ...     ''',
            ...     data=[
            ...         {"month": "Jan", "users": 1200},
            ...         {"month": "Feb", "users": 1350},
            ...         {"month": "Mar", "users": 1500},
            ...     ],
            ...     source_queries=["SELECT month, count(*) as users FROM..."]
            ... )
        """
        # Import here to avoid circular imports
        from apps.artifacts.models import Artifact

        # Validate artifact type
        if artifact_type not in VALID_ARTIFACT_TYPES:
            return {
                "artifact_id": None,
                "status": "error",
                "title": title,
                "type": artifact_type,
                "render_url": None,
                "message": f"Invalid artifact_type '{artifact_type}'. "
                          f"Must be one of: {', '.join(sorted(VALID_ARTIFACT_TYPES))}",
            }

        # Validate code is provided
        if not code or not code.strip():
            return {
                "artifact_id": None,
                "status": "error",
                "title": title,
                "type": artifact_type,
                "render_url": None,
                "message": "Code is required. Please provide the artifact source code.",
            }

        # Validate title
        if not title or not title.strip():
            return {
                "artifact_id": None,
                "status": "error",
                "title": title,
                "type": artifact_type,
                "render_url": None,
                "message": "Title is required. Please provide a descriptive title.",
            }

        try:
            artifact = Artifact.objects.create(
                project=project,
                created_by=user,
                title=title.strip(),
                description=description.strip() if description else "",
                artifact_type=artifact_type,
                code=code,
                data=data or {},
                version=1,
                conversation_id=conversation_id or "",
            )

            # Store source queries in the data field if provided
            # (the model doesn't have a dedicated field, so we include in data)
            if source_queries:
                artifact_data = artifact.data or {}
                artifact_data["_source_queries"] = source_queries
                artifact.data = artifact_data
                artifact.save(update_fields=["data"])

            logger.info(
                "Created artifact %s for project %s: %s",
                artifact.id,
                project.slug,
                title,
            )

            # Build render URL
            render_url = f"/artifacts/{artifact.id}/render/"

            return {
                "artifact_id": str(artifact.id),
                "status": "created",
                "title": artifact.title,
                "type": artifact.artifact_type,
                "render_url": render_url,
                "message": f"Artifact '{title}' created successfully.",
            }

        except Exception as e:
            logger.exception(
                "Failed to create artifact for project %s: %s",
                project.slug,
                str(e),
            )
            return {
                "artifact_id": None,
                "status": "error",
                "title": title,
                "type": artifact_type,
                "render_url": None,
                "message": f"Failed to create artifact: {str(e)}",
            }

    @tool
    def update_artifact(
        artifact_id: str,
        code: str,
        title: str | None = None,
        data: dict | None = None,
    ) -> dict[str, Any]:
        """
        Update an existing artifact by creating a new version.

        Use this tool when the user wants to modify an existing artifact,
        such as changing the visualization, updating data, or fixing issues.
        This preserves the previous version in the version history.

        Args:
            artifact_id: UUID of the artifact to update (from create_artifact response).

            code: New source code for the artifact. Same format as create_artifact.

            title: Optional new title. If not provided, keeps the existing title.

            data: Optional new data payload. If not provided, keeps the existing data.
                Set to an empty dict {} to clear the data.

        Returns:
            A dict containing:
            - artifact_id: UUID of the NEW artifact version (as string)
            - previous_version_id: UUID of the previous version
            - status: "updated" on success, "error" on failure
            - version: New version number
            - title: The artifact title
            - render_url: URL path to render the new version
            - message: Success or error message

        Example:
            >>> update_artifact(
            ...     artifact_id="123e4567-e89b-12d3-a456-426614174000",
            ...     code='''
            ...     // Updated chart with better styling
            ...     import { LineChart, Line, XAxis, YAxis } from "recharts";
            ...     export default function Chart({ data }) { ... }
            ...     ''',
            ...     data=[{"month": "Jan", "users": 1250}, ...]  # Updated data
            ... )
        """
        # Import here to avoid circular imports
        from apps.artifacts.models import Artifact, ArtifactVersion

        # Validate code is provided
        if not code or not code.strip():
            return {
                "artifact_id": None,
                "previous_version_id": artifact_id,
                "status": "error",
                "version": None,
                "title": None,
                "render_url": None,
                "message": "Code is required. Please provide the updated artifact source code.",
            }

        try:
            # Find the existing artifact
            try:
                original = Artifact.objects.get(id=artifact_id, project=project)
            except Artifact.DoesNotExist:
                return {
                    "artifact_id": None,
                    "previous_version_id": artifact_id,
                    "status": "error",
                    "version": None,
                    "title": None,
                    "render_url": None,
                    "message": f"Artifact with ID '{artifact_id}' not found in this project.",
                }

            # Save current state to version history
            ArtifactVersion.objects.create(
                artifact=original,
                version_number=original.version,
                code=original.code,
                data=original.data,
                created_by=original.created_by,
            )

            # Update the artifact
            previous_version = original.version
            original.code = code
            original.version = previous_version + 1

            if title is not None:
                original.title = title.strip()

            if data is not None:
                original.data = data

            # Update the user who made this change
            original.created_by = user

            original.save()

            logger.info(
                "Updated artifact %s to version %d for project %s",
                original.id,
                original.version,
                project.slug,
            )

            # Build render URL
            render_url = f"/artifacts/{original.id}/render/"

            return {
                "artifact_id": str(original.id),
                "previous_version_id": artifact_id,
                "status": "updated",
                "version": original.version,
                "title": original.title,
                "render_url": render_url,
                "message": f"Artifact '{original.title}' updated to version {original.version}.",
            }

        except Exception as e:
            logger.exception(
                "Failed to update artifact %s for project %s: %s",
                artifact_id,
                project.slug,
                str(e),
            )
            return {
                "artifact_id": None,
                "previous_version_id": artifact_id,
                "status": "error",
                "version": None,
                "title": None,
                "render_url": None,
                "message": f"Failed to update artifact: {str(e)}",
            }

    # Set tool names explicitly
    create_artifact.name = "create_artifact"
    update_artifact.name = "update_artifact"

    return [create_artifact, update_artifact]


__all__ = [
    "create_artifact_tools",
    "VALID_ARTIFACT_TYPES",
]
