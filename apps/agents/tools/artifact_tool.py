"""
Artifact creation tools for the Scout data agent platform.

Factory functions for agent tools that create and version interactive artifacts
(React, HTML, Markdown, Plotly, SVG), optionally linked to source SQL queries.
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
    code: str | None = None
    description: str = ""
    data: dict | None = None
    source_queries: list[dict[str, str]] | None = Field(default=None)


class UpdateArtifactInput(BaseModel):
    artifact_id: str
    code: str | None = None
    title: str | None = None
    data: dict | None = None
    source_queries: list[dict[str, str]] | None = Field(default=None)


VALID_ARTIFACT_TYPES = frozenset(
    {
        "react",
        "html",
        "markdown",
        "plotly",
        "svg",
    }
)


def create_artifact_tools(
    workspace: "Workspace", user: "User | None", conversation_id: str | None = None
) -> list:
    """Create the [create_artifact, update_artifact] tools scoped to a workspace.

    ``conversation_id`` is recorded on created artifacts for provenance.
    """

    @tool(args_schema=CreateArtifactInput)
    async def create_artifact(
        title, artifact_type, code, description="", data=None, source_queries=None
    ) -> dict[str, Any]:
        """
        Create a new interactive artifact (visualization, chart, or content).

        Use this tool when the user needs a visual representation of data,
        such as charts, tables, dashboards, or formatted content. The artifact
        will be rendered in an interactive preview.

        IMPORTANT: For data-driven artifacts, always provide source_queries with
        the SQL queries that produce the data the component needs. The artifact
        will execute these queries at render time to fetch live data. Do NOT
        embed query results in the data parameter -- instead, write your
        component to consume data keyed by the query name.

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
                - For "react": JSX code with a default export component.
                  The component receives a `data` prop whose keys match the
                  query names from source_queries. For example, if you provide
                  a query named "monthly_revenue", access it as data.monthly_revenue
                  (an array of objects with column-name keys).
                - For "plotly": JSON string of Plotly figure specification
                - For "html": HTML markup
                - For "markdown": Markdown text
                - For "svg": SVG markup

            description: Optional description of what this artifact visualizes.
                Helps users understand the artifact's purpose.

            data: Optional static JSON data to pass to the artifact. For
                data-driven artifacts, prefer source_queries instead so the
                artifact always shows live data. Use this only for non-query
                configuration (e.g., color schemes, labels, thresholds).

            source_queries: List of named SQL queries that provide live data
                to the artifact. Each entry is a dict with "name" and "sql"
                keys. The queries are executed at render time against the
                workspace database, and results are passed to the component
                under data[name].

                Example:
                    [
                        {"name": "monthly_revenue", "sql": "SELECT ..."},
                        {"name": "top_products", "sql": "SELECT ..."}
                    ]

                The component then accesses data.monthly_revenue (array of
                row objects) and data.top_products.

        Returns:
            A dict containing:
            - artifact_id: UUID of the created artifact (as string)
            - status: "created" on success, "error" on failure
            - title: The artifact title
            - type: The artifact type
            - render_url: URL path to render the artifact
            - message: Success or error message
        """
        from apps.artifacts.models import Artifact  # avoid circular import

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

        if not code or not code.strip():
            return {
                "artifact_id": None,
                "status": "error",
                "title": title,
                "type": artifact_type,
                "render_url": None,
                "message": "Code is required. Please provide the artifact source code.",
            }

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
            )

            logger.info(
                "Created artifact %s for workspace %s: %s",
                artifact.id,
                workspace.id,
                title,
            )

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
        artifact_id, code, title=None, data=None, source_queries=None
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

            source_queries: Optional new list of named SQL queries. Same format
                as create_artifact. If not provided, keeps existing queries.

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
        from apps.artifacts.models import Artifact  # avoid circular import

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

            # Fall back to the original when the caller omitted a field.
            new_title = title.strip() if title is not None else original.title
            new_data = data if data is not None else original.data
            new_source_queries = (
                source_queries if source_queries is not None else original.source_queries
            )

            # No-op guard (arch #254, finding 09#9): each update copies the full
            # code into a new row, so "updates" with no change accrete copies
            # indefinitely. Return the existing artifact instead.
            if (
                code == original.code
                and new_title == original.title
                and new_data == original.data
                and new_source_queries == original.source_queries
            ):
                logger.info(
                    "update_artifact no-op for %s (no change) — skipping version copy",
                    original.id,
                )
                render_url = f"/api/workspaces/{workspace.id}/artifacts/{original.id}/sandbox/"
                return {
                    "artifact_id": str(original.id),
                    "previous_version_id": artifact_id,
                    "status": "updated",
                    "version": original.version,
                    "title": original.title,
                    "render_url": render_url,
                    "message": f"Artifact '{original.title}' is already up to date.",
                }

            new_artifact = Artifact(
                workspace=workspace,
                created_by=user,
                title=new_title,
                description=original.description,
                artifact_type=original.artifact_type,
                code=code,
                data=new_data,
                version=original.version + 1,
                parent_artifact=original,
                conversation_id=original.conversation_id,
                source_queries=new_source_queries,
            )
            await new_artifact.asave()

            logger.info(
                "Created artifact version %s (v%d) from %s for workspace %s",
                new_artifact.id,
                new_artifact.version,
                original.id,
                workspace.id,
            )

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

    create_artifact.name = "create_artifact"
    update_artifact.name = "update_artifact"

    return [create_artifact, update_artifact]


__all__ = [
    "VALID_ARTIFACT_TYPES",
    "create_artifact_tools",
]
