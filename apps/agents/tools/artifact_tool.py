"""
Artifact creation tools for the Scout data agent platform.

This module provides factory functions to create tools that allow the agent
to generate interactive visualizations and content artifacts. Artifacts can be
React components, HTML, Markdown, Plotly charts, or SVG graphics.

The tools support:
- Creating new artifacts with code and optional data
- Updating existing artifacts (creates new versions preserving history)
- Linking story artifacts to semantic query specs for provenance tracking
"""

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)


class CreateArtifactInput(BaseModel):
    title: str
    artifact_type: str
    code: str = ""
    description: str = ""
    data: dict | None = None
    source_queries: list[dict[str, str]] | None = Field(default=None)
    semantic_queries: list[dict[str, Any]] | None = Field(default=None)


class UpdateArtifactInput(BaseModel):
    artifact_id: str
    code: str = ""
    title: str | None = None
    data: dict | None = None
    source_queries: list[dict[str, str]] | None = Field(default=None)
    semantic_queries: list[dict[str, Any]] | None = Field(default=None)


# Valid artifact types that can be created
VALID_ARTIFACT_TYPES = frozenset(
    {
        "react",
        "html",
        "markdown",
        "plotly",
        "svg",
        "story",
    }
)


def create_artifact_tools(
    workspace: "Workspace", user: "User | None", conversation_id: str | None = None
) -> list:
    """
    Factory function to create artifact creation tools for a specific workspace.

    Creates two tools:
    1. create_artifact: Create a new artifact with code and optional data
    2. update_artifact: Create a new version of an existing artifact

    Args:
        workspace: The Workspace model instance for scoping artifacts.
        user: The User model instance who triggered the conversation.
              Used to track artifact ownership.
        conversation_id: The conversation/thread ID for tracking artifact provenance.

    Returns:
        A list of LangChain tool functions [create_artifact, update_artifact].
    """

    @tool(args_schema=CreateArtifactInput)
    async def create_artifact(
        title,
        artifact_type,
        code="",
        description="",
        data=None,
        source_queries=None,
        semantic_queries=None,
    ) -> dict[str, Any]:
        """
        Create a new interactive artifact (visualization, chart, or content).

        Use this tool when the user needs a visual representation of data,
        such as charts, tables, dashboards, or formatted content. The artifact
        will be rendered in an interactive preview.

        IMPORTANT: For data-driven artifacts, create a "story" artifact and
        provide semantic_queries with structured measure/dimension query specs.
        Do NOT embed query results in the data parameter.

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
                - "story": Structured semantic story document (recommended
                  for data-backed charts, tables, and reports).

            code: The source code for the artifact:
                - For "react": JSX code with a default export component.
                  Legacy data-backed React artifacts receive a `data` prop.
                - For "plotly": JSON string of Plotly figure specification
                - For "html": HTML markup
                - For "markdown": Markdown text
                - For "svg": SVG markup
                - For "story": leave code blank and provide data.story_doc.

            description: Optional description of what this artifact visualizes.
                Helps users understand the artifact's purpose.

            data: Optional static JSON data to pass to the artifact. Story
                artifacts require data.story_doc and should put live-data
                requests in semantic_queries.

            semantic_queries: List of named semantic query specs that provide
                live data to a story artifact. Each entry must include "name"
                plus semantic_query arguments such as measures, dimensions,
                filters, time_dimension, granularity, and limit.

        Returns:
            A dict containing:
            - artifact_id: UUID of the created artifact (as string)
            - status: "created" on success, "error" on failure
            - title: The artifact title
            - type: The artifact type
            - render_url: URL path to render the artifact
            - message: Success or error message
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

        # Validate code/story content is provided
        if artifact_type == "story":
            story_doc = (data or {}).get("story_doc") if isinstance(data, dict) else None
            if not story_doc:
                return {
                    "artifact_id": None,
                    "status": "error",
                    "title": title,
                    "type": artifact_type,
                    "render_url": None,
                    "message": "Story artifacts require data.story_doc.",
                }
        elif not code or not code.strip():
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
            artifact = await Artifact.objects.acreate(
                workspace=workspace,
                created_by=user,
                title=title.strip(),
                description=description.strip() if description else "",
                artifact_type=artifact_type,
                code=code,
                data=data or {},
                version=1,
                conversation_id=conversation_id or "",
                source_queries=source_queries or [],
                semantic_queries=semantic_queries or [],
            )

            logger.info(
                "Created artifact %s for workspace %s: %s",
                artifact.id,
                workspace.id,
                title,
            )

            # Build render URL pointing at the real sandbox route
            # (/api/workspaces/<wsid>/artifacts/<id>/sandbox/).
            render_url = f"/api/workspaces/{workspace.id}/artifacts/{artifact.id}/sandbox/"

            return {
                "artifact_id": str(artifact.id),
                "status": "created",
                "title": artifact.title,
                "type": artifact.artifact_type,
                "render_url": render_url,
                "message": f"Artifact '{title}' created successfully.",
            }

        except Exception as e:
            logger.exception("Failed to create artifact for workspace %s", workspace.id)
            return {
                "artifact_id": None,
                "status": "error",
                "title": title,
                "type": artifact_type,
                "render_url": None,
                "message": f"Failed to create artifact: {e!s}",
            }

    @tool(args_schema=UpdateArtifactInput)
    async def update_artifact(
        artifact_id, code="", title=None, data=None, source_queries=None, semantic_queries=None
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

            semantic_queries: Optional new list of named semantic query specs.
                If not provided, keeps existing semantic queries.

        Returns:
            A dict containing:
            - artifact_id: UUID of the NEW artifact version (as string)
            - previous_version_id: UUID of the previous version
            - status: "updated" on success, "error" on failure
            - version: New version number
            - title: The artifact title
            - render_url: URL path to render the new version
            - message: Success or error message
        """
        # Import here to avoid circular imports
        from apps.artifacts.models import Artifact

        try:
            # Find the existing artifact
            try:
                original = await Artifact.objects.aget(id=artifact_id, workspace=workspace)
            except Artifact.DoesNotExist:
                return {
                    "artifact_id": None,
                    "previous_version_id": artifact_id,
                    "status": "error",
                    "version": None,
                    "title": None,
                    "render_url": None,
                    "message": f"Artifact with ID '{artifact_id}' not found in this workspace.",
                }

            if original.artifact_type == "story":
                next_data = data if data is not None else original.data
                story_doc = next_data.get("story_doc") if isinstance(next_data, dict) else None
                if not story_doc:
                    return {
                        "artifact_id": None,
                        "previous_version_id": artifact_id,
                        "status": "error",
                        "version": None,
                        "title": None,
                        "render_url": None,
                        "message": "Story artifacts require data.story_doc.",
                    }
            elif not code or not code.strip():
                return {
                    "artifact_id": None,
                    "previous_version_id": artifact_id,
                    "status": "error",
                    "version": None,
                    "title": None,
                    "render_url": None,
                    "message": "Code is required. Please provide the updated artifact source code.",
                }

            # Create a new version linked to the original
            new_artifact = Artifact(
                workspace=workspace,
                created_by=user,
                title=title.strip() if title is not None else original.title,
                description=original.description,
                artifact_type=original.artifact_type,
                code=code if code is not None else original.code,
                data=data if data is not None else original.data,
                version=original.version + 1,
                parent_artifact=original,
                conversation_id=original.conversation_id,
                source_queries=source_queries
                if source_queries is not None
                else original.source_queries,
                semantic_queries=semantic_queries
                if semantic_queries is not None
                else original.semantic_queries,
            )
            await new_artifact.asave()

            logger.info(
                "Created artifact version %s (v%d) from %s for workspace %s",
                new_artifact.id,
                new_artifact.version,
                original.id,
                workspace.id,
            )

            # Build render URL pointing at the real sandbox route
            # (/api/workspaces/<wsid>/artifacts/<id>/sandbox/).
            render_url = f"/api/workspaces/{workspace.id}/artifacts/{new_artifact.id}/sandbox/"

            return {
                "artifact_id": str(new_artifact.id),
                "previous_version_id": artifact_id,
                "status": "updated",
                "version": new_artifact.version,
                "title": new_artifact.title,
                "render_url": render_url,
                "message": f"Artifact '{new_artifact.title}' updated to version {new_artifact.version}.",
            }

        except Exception as e:
            logger.exception(
                "Failed to update artifact %s for workspace %s", artifact_id, workspace.id
            )
            return {
                "artifact_id": None,
                "previous_version_id": artifact_id,
                "status": "error",
                "version": None,
                "title": None,
                "render_url": None,
                "message": f"Failed to update artifact: {e!s}",
            }

    # Set tool names explicitly
    create_artifact.name = "create_artifact"
    update_artifact.name = "update_artifact"

    return [create_artifact, update_artifact]


__all__ = [
    "VALID_ARTIFACT_TYPES",
    "create_artifact_tools",
]
