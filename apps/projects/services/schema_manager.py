"""
Schema Manager for the Scout-managed database.

Creates and tears down tenant-scoped PostgreSQL schemas.
"""

from __future__ import annotations

import logging

import psycopg2
from django.conf import settings

from apps.projects.models import SchemaState, TenantSchema

logger = logging.getLogger(__name__)


def get_managed_db_connection():
    """Get a psycopg2 connection to the managed database."""
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise RuntimeError("MANAGED_DATABASE_URL is not configured")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


class SchemaManager:
    """Creates and manages tenant schemas in the managed database."""

    def provision(self, tenant_membership) -> TenantSchema:
        """Get or create a schema for the tenant."""
        existing = TenantSchema.objects.filter(
            tenant_membership=tenant_membership,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).first()

        if existing:
            existing.save(update_fields=["last_accessed_at"])  # touch
            return existing

        schema_name = self._sanitize_schema_name(tenant_membership.tenant_id)

        ts = TenantSchema.objects.create(
            tenant_membership=tenant_membership,
            schema_name=schema_name,
            state=SchemaState.PROVISIONING,
        )

        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"CREATE SCHEMA IF NOT EXISTS "
                f"{psycopg2.extensions.quote_ident(schema_name, cursor)}"
            )
            cursor.close()
        finally:
            conn.close()

        ts.state = SchemaState.ACTIVE
        ts.save(update_fields=["state"])

        logger.info(
            "Provisioned schema '%s' for tenant '%s'",
            schema_name,
            tenant_membership.tenant_id,
        )
        return ts

    def teardown(self, tenant_schema: TenantSchema) -> None:
        """Drop a tenant's schema and mark it as torn down."""
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"DROP SCHEMA IF EXISTS "
                f"{psycopg2.extensions.quote_ident(tenant_schema.schema_name, cursor)} CASCADE"
            )
            cursor.close()
        finally:
            conn.close()

        tenant_schema.state = SchemaState.TEARDOWN
        tenant_schema.save(update_fields=["state"])

    def _sanitize_schema_name(self, tenant_id: str) -> str:
        """Convert a tenant_id to a valid PostgreSQL schema name."""
        name = tenant_id.lower().replace("-", "_")
        name = "".join(c for c in name if c.isalnum() or c == "_")
        if name and name[0].isdigit():
            name = f"t_{name}"
        return name or "unknown"
