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
        """Get or create a schema for the tenant.

        Checks for an existing active schema by schema_name (not just by
        tenant_membership) so that multiple users in the same CommCare domain
        share one schema rather than colliding on the unique constraint.
        """
        from django.db import IntegrityError

        schema_name = self._sanitize_schema_name(tenant_membership.tenant_id)

        existing = TenantSchema.objects.filter(
            schema_name=schema_name,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).first()

        if existing:
            existing.save(update_fields=["last_accessed_at"])  # touch
            return existing

        try:
            ts = TenantSchema.objects.create(
                tenant_membership=tenant_membership,
                schema_name=schema_name,
                state=SchemaState.PROVISIONING,
            )
        except IntegrityError:
            # Race condition: another process created the record between our
            # filter and create. Re-fetch and return it.
            ts = TenantSchema.objects.get(schema_name=schema_name)
            if ts.state in (SchemaState.ACTIVE, SchemaState.MATERIALIZING):
                return ts
            # Fall through: record exists but isn't active yet; let this
            # caller attempt the CREATE SCHEMA (IF NOT EXISTS is safe).

        try:
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
        except Exception:
            # Clean up the PROVISIONING record so the next attempt can retry
            # rather than hitting the unique constraint.
            ts.delete()
            raise

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
