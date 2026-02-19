"""Context for the MCP server.

Holds configuration as an immutable snapshot. Supports both legacy project-based
context and new tenant-based context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


@dataclass(frozen=True)
class TenantContext:
    """Immutable snapshot of tenant context for tool handlers."""

    tenant_id: str
    user_id: str
    provider: str
    schema_name: str
    oauth_tokens: dict[str, str] = field(default_factory=dict)
    max_rows_per_query: int = 500
    max_query_timeout_seconds: int = 30


async def load_project_context(project_id: str) -> ProjectContext:
    """Load a ProjectContext from the database by project ID.

    Raises ValueError if the project is not found, not active, or its
    database connection is inactive.
    """
    from apps.projects.models import Project

    try:
        project = await Project.objects.select_related("database_connection").aget(
            id=project_id, is_active=True
        )
    except Project.DoesNotExist as e:
        raise ValueError(f"Project '{project_id}' not found or not active") from e

    if not project.database_connection.is_active:
        raise ValueError(f"Database connection for project '{project.name}' is not active")

    return ProjectContext.from_project(project)


async def load_tenant_context(tenant_id: str) -> ProjectContext:
    """Load a ProjectContext for a tenant from the managed database.

    Uses the tenant_id (domain name) to find the TenantSchema and builds
    a ProjectContext pointing at the managed DB with the tenant's schema.

    Raises ValueError if the tenant schema is not found or not active.
    """
    from urllib.parse import urlparse

    from asgiref.sync import sync_to_async
    from django.conf import settings

    from apps.projects.models import SchemaState, TenantSchema

    ts = await TenantSchema.objects.filter(
        tenant_membership__tenant_id=tenant_id,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()

    if ts is None:
        raise ValueError(
            f"No active schema for tenant '{tenant_id}'. "
            f"Run materialization first to load data."
        )

    # Parse MANAGED_DATABASE_URL into connection params
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise ValueError("MANAGED_DATABASE_URL is not configured")

    connection_params = await sync_to_async(_parse_db_url)(url, ts.schema_name)

    return ProjectContext(
        project_id=f"tenant:{tenant_id}",
        project_name=tenant_id,
        db_schema=ts.schema_name,
        allowed_tables=[],
        excluded_tables=[],
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        readonly_role="",
        connection_params=connection_params,
    )


def _parse_db_url(url: str, schema: str) -> dict:
    """Parse a database URL into psycopg2 connection params."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") or "scout",
        "user": parsed.username or "",
        "password": parsed.password or "",
        "options": f"-c search_path={schema},public -c statement_timeout=30000",
    }
