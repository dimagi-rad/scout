"""Context for the MCP server.

Holds configuration as an immutable snapshot for tenant-based queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class QueryContext:
    """Immutable snapshot of tenant query configuration for tool handlers."""

    tenant_id: str
    schema_name: str
    max_rows_per_query: int = 500
    max_query_timeout_seconds: int = 30
    connection_params: dict[str, Any] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class TenantContext:
    """Immutable snapshot of tenant context for tool handlers."""

    tenant_id: str
    user_id: str
    provider: str
    schema_name: str
    oauth_tokens: dict[str, str] = None  # type: ignore[assignment]
    max_rows_per_query: int = 500
    max_query_timeout_seconds: int = 30


async def load_tenant_context(tenant_id: str) -> QueryContext:
    """Load a QueryContext for a tenant from the managed database.

    Uses the tenant_id (domain name) to find the TenantSchema and builds
    a QueryContext pointing at the managed DB with the tenant's schema.
    Resets the schema's inactivity TTL via touch().

    Raises ValueError if the tenant schema is not found or not active.
    """
    from asgiref.sync import sync_to_async
    from django.conf import settings

    from apps.projects.models import SchemaState, TenantSchema

    ts = await TenantSchema.objects.filter(
        tenant__external_id=tenant_id,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()

    if ts is None:
        raise ValueError(
            f"No active schema for tenant '{tenant_id}'. Run materialization first to load data."
        )

    await sync_to_async(ts.touch)()

    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise ValueError("MANAGED_DATABASE_URL is not configured")

    connection_params = await sync_to_async(_parse_db_url)(url, ts.schema_name)

    return QueryContext(
        tenant_id=tenant_id,
        schema_name=ts.schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params=connection_params,
    )


async def load_workspace_context(workspace_id: str) -> QueryContext:
    """Load a QueryContext for a workspace, routing correctly for multi-tenant.

    - Single-tenant workspace (1 tenant): delegates to load_tenant_context(tenant.external_id).
    - Multi-tenant workspace (2+ tenants): uses the WorkspaceViewSchema.

    Raises ValueError if the workspace has no tenants, or if multi-tenant and
    no active WorkspaceViewSchema exists.
    """
    from asgiref.sync import sync_to_async
    from django.conf import settings

    from apps.projects.models import SchemaState, Workspace, WorkspaceViewSchema

    try:
        workspace = await Workspace.objects.aget(id=workspace_id)
    except Workspace.DoesNotExist:
        raise ValueError(f"Workspace '{workspace_id}' not found") from None

    tenant_count = await workspace.tenants.acount()

    if tenant_count == 0:
        raise ValueError(f"Workspace '{workspace_id}' has no tenants")

    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        return await load_tenant_context(tenant.external_id)

    # Multi-tenant: use the view schema
    try:
        vs = await WorkspaceViewSchema.objects.aget(
            workspace_id=workspace_id,
            state=SchemaState.ACTIVE,
        )
    except WorkspaceViewSchema.DoesNotExist:
        raise ValueError(
            f"No active view schema for workspace '{workspace_id}'. "
            "Trigger a rebuild via POST /api/workspaces/<id>/tenants/ or a data refresh."
        ) from None

    await sync_to_async(vs.touch)()

    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise ValueError("MANAGED_DATABASE_URL is not configured")

    connection_params = await sync_to_async(_parse_db_url)(url, vs.schema_name)

    return QueryContext(
        tenant_id=workspace_id,
        schema_name=vs.schema_name,
        max_rows_per_query=500,
        max_query_timeout_seconds=30,
        connection_params=connection_params,
    )


def _parse_db_url(url: str, schema: str) -> dict:
    """Parse a database URL into psycopg connection params."""
    # Defensive validation: schema_name must only contain safe characters before
    # embedding in the options string. _sanitize_schema_name already guarantees
    # this, but we re-check here as defence-in-depth.
    if not re.match(r"^[a-z][a-z0-9_]*$", schema):
        raise ValueError(f"Invalid schema name: {schema!r}")

    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") or "scout",
        "user": parsed.username or "",
        "password": parsed.password or "",
        # schema has been validated against ^[a-z][a-z0-9_]*$ above — safe to interpolate
        "options": f"-c search_path={schema},public -c statement_timeout=30000",
    }
