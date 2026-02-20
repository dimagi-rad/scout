"""Context for the MCP server.

Holds configuration as an immutable snapshot for tenant-based queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    Raises ValueError if the tenant schema is not found or not active.
    """
    from asgiref.sync import sync_to_async
    from django.conf import settings

    from apps.projects.models import SchemaState, TenantSchema

    ts = await TenantSchema.objects.filter(
        tenant_membership__tenant_id=tenant_id,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).afirst()

    if ts is None:
        raise ValueError(
            f"No active schema for tenant '{tenant_id}'. Run materialization first to load data."
        )

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


def _parse_db_url(url: str, schema: str) -> dict:
    """Parse a database URL into psycopg2 connection params."""
    import re
    from urllib.parse import urlparse

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
        "options": f"-c search_path={schema},public -c statement_timeout=30000",
    }
