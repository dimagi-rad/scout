"""MCP tool for listing available Scout projects."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from apps.projects.models import Project

logger = logging.getLogger(__name__)


def register_list_projects(mcp: FastMCP) -> None:
    """Register the list_projects tool on the MCP server."""

    @mcp.tool()
    def list_projects() -> list[dict]:
        """
        List all Scout projects available for querying.

        Returns project slugs, names, and table counts so you know which
        project_slug to pass to other tools.

        Returns:
            A list of dicts with slug, name, description, schema, and table_count.
        """
        projects = Project.objects.filter(
            database_connection__isnull=False,
            database_connection__is_active=True,
        ).select_related("database_connection")

        results = []
        for p in projects:
            dd = p.data_dictionary or {}
            table_count = len(dd.get("tables", {}))
            results.append({
                "slug": p.slug,
                "name": p.name,
                "description": p.description or "",
                "schema": p.db_schema,
                "table_count": table_count,
            })

        return results
