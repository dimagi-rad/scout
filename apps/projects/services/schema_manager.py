"""
Schema Manager for the Scout-managed database.

Creates and tears down tenant-scoped PostgreSQL schemas.
"""

from __future__ import annotations

import logging
import re
import uuid

import psycopg
import psycopg.sql
from django.conf import settings

from apps.projects.models import SchemaState, TenantSchema, WorkspaceViewSchema

logger = logging.getLogger(__name__)


def get_managed_db_connection():
    """Get a psycopg connection to the managed database."""
    url = settings.MANAGED_DATABASE_URL
    if not url:
        raise RuntimeError("MANAGED_DATABASE_URL is not configured")
    return psycopg.connect(url, autocommit=True)


class SchemaManager:
    """Creates and manages tenant schemas in the managed database."""

    def provision(self, tenant) -> TenantSchema:
        """Get or create a schema for the tenant.

        Checks for an existing active schema by schema_name so that multiple
        users in the same tenant share one schema rather than colliding on the
        unique constraint.
        """
        from django.db import IntegrityError

        schema_name = self._sanitize_schema_name(tenant.external_id)

        existing = TenantSchema.objects.filter(
            schema_name=schema_name,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).first()

        if existing:
            existing.touch()
            return existing

        try:
            ts = TenantSchema.objects.create(
                tenant=tenant,
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
                    psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        psycopg.sql.Identifier(schema_name)
                    )
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
            tenant.external_id,
        )
        return ts

    def create_physical_schema(self, tenant_schema: TenantSchema) -> None:
        """Create the physical PostgreSQL schema for an existing TenantSchema record.

        Idempotent — uses ``CREATE SCHEMA IF NOT EXISTS``. The caller is
        responsible for updating ``tenant_schema.state`` on success or failure.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    psycopg.sql.Identifier(tenant_schema.schema_name)
                )
            )
            cursor.close()
        finally:
            conn.close()

    def create_refresh_schema(self, tenant) -> TenantSchema:
        """Create a new TenantSchema record for a background refresh.

        Returns a PROVISIONING record with a unique schema name. The caller
        is responsible for creating the physical schema and dispatching the
        Celery task (refresh_tenant_schema) to run the materialization.
        """
        schema_name = f"{self._sanitize_schema_name(tenant.external_id)}_r{uuid.uuid4().hex[:8]}"
        return TenantSchema.objects.create(
            tenant=tenant,
            schema_name=schema_name,
            state=SchemaState.PROVISIONING,
        )

    def teardown(self, tenant_schema: TenantSchema) -> None:
        """Drop a tenant's schema from the managed database.

        Only performs the physical DROP SCHEMA — callers are responsible for
        updating the model state (EXPIRED or FAILED) after this returns.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(tenant_schema.schema_name)
                )
            )
            cursor.close()
        finally:
            conn.close()

    def _view_schema_name(self, workspace_id) -> str:
        """Generate a PostgreSQL schema name for a workspace's view schema."""
        hex_id = str(workspace_id).replace("-", "")[:16]
        return f"ws_{hex_id}"

    def build_view_schema(self, workspace) -> WorkspaceViewSchema:
        """Create (or replace) the PostgreSQL view schema for a multi-tenant workspace.

        Fetches all active TenantSchema objects for the workspace's tenants,
        collects their tables and columns, then creates UNION ALL views in a
        dedicated schema. Raises ValueError if any tenant has no active schema.

        Returns the WorkspaceViewSchema model instance with state=ACTIVE on success.
        """
        tenants = list(workspace.tenants.all())
        if not tenants:
            raise ValueError(f"Workspace {workspace.id} has no tenants")

        # Bulk-fetch active TenantSchema records for all tenants in one query
        active_schemas = {
            ts.tenant_id: ts
            for ts in TenantSchema.objects.filter(tenant__in=tenants, state=SchemaState.ACTIVE)
        }
        tenant_schemas: list[tuple[str, str]] = []  # (schema_name, tenant_external_id)
        for tenant in tenants:
            ts = active_schemas.get(tenant.id)
            if ts is None:
                raise ValueError(
                    f"Tenant '{tenant.external_id}' has no active schema. "
                    "Run a data refresh for this tenant before building the view schema."
                )
            tenant_schemas.append((ts.schema_name, tenant.external_id))

        view_schema_name = self._view_schema_name(workspace.id)

        # Get or create the WorkspaceViewSchema record
        vs, _ = WorkspaceViewSchema.objects.get_or_create(
            workspace=workspace,
            defaults={"schema_name": view_schema_name, "state": SchemaState.PROVISIONING},
        )
        if vs.schema_name != view_schema_name:
            vs.schema_name = view_schema_name
        vs.state = SchemaState.PROVISIONING
        vs.save(update_fields=["schema_name", "state"])

        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()

            # Validate schema name before embedding
            if not re.match(r"^ws_[a-f0-9]{16}$", view_schema_name):
                raise ValueError(f"Invalid view schema name: {view_schema_name!r}")

            # Step 1: Create the physical schema
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    psycopg.sql.Identifier(view_schema_name)
                )
            )

            # Step 2: Collect tables and columns per tenant schema
            all_tables: dict[str, dict[str, list[str]]] = {}
            # all_tables[table_name][schema_name] = [col1, col2, ...]

            for schema_name, _ in tenant_schemas:
                cursor.execute(
                    "SELECT table_name, column_name "
                    "FROM information_schema.columns "
                    "WHERE table_schema = %s "
                    "ORDER BY table_name, ordinal_position",
                    (schema_name,),
                )
                for table_name, col_name in cursor.fetchall():
                    all_tables.setdefault(table_name, {})
                    all_tables[table_name].setdefault(schema_name, []).append(col_name)

            # Step 3: Build UNION ALL views
            for table_name, schema_cols in all_tables.items():
                # Union of all column names (preserving first-seen order)
                seen: set[str] = set()
                union_cols: list[str] = []
                for schema_name, _ in tenant_schemas:
                    for col in schema_cols.get(schema_name, []):
                        if col not in seen:
                            union_cols.append(col)
                            seen.add(col)

                # Build one SELECT per tenant schema that has this table
                select_parts: list[psycopg.sql.Composed] = []
                for schema_name, tenant_external_id in tenant_schemas:
                    if schema_name not in schema_cols:
                        continue  # This tenant doesn't have this table — skip
                    existing = set(schema_cols[schema_name])
                    select_list = []
                    for col in union_cols:
                        if col in existing:
                            select_list.append(
                                psycopg.sql.SQL("{}.{}").format(
                                    psycopg.sql.Identifier(table_name),
                                    psycopg.sql.Identifier(col),
                                )
                            )
                        else:
                            select_list.append(
                                psycopg.sql.SQL("NULL AS {}").format(psycopg.sql.Identifier(col))
                            )
                    select_list.append(
                        psycopg.sql.SQL("{} AS _tenant").format(
                            psycopg.sql.Literal(tenant_external_id)
                        )
                    )
                    select_parts.append(
                        psycopg.sql.SQL("SELECT {} FROM {}.{}").format(
                            psycopg.sql.SQL(", ").join(select_list),
                            psycopg.sql.Identifier(schema_name),
                            psycopg.sql.Identifier(table_name),
                        )
                    )

                if not select_parts:
                    continue

                view_sql = psycopg.sql.SQL("CREATE OR REPLACE VIEW {}.{} AS {}").format(
                    psycopg.sql.Identifier(view_schema_name),
                    psycopg.sql.Identifier(table_name),
                    psycopg.sql.SQL(" UNION ALL ").join(select_parts),
                )
                cursor.execute(view_sql)

            cursor.close()
        except Exception:
            # Drop any partially-created schema before marking FAILED to avoid leaving debris
            try:
                if not conn.closed:
                    c = conn.cursor()
                    c.execute(
                        psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                            psycopg.sql.Identifier(view_schema_name)
                        )
                    )
                    c.close()
            except Exception:
                logger.exception(
                    "Failed to drop partial view schema '%s' during cleanup", view_schema_name
                )
            if not conn.closed:
                conn.close()
            vs.state = SchemaState.FAILED
            vs.save(update_fields=["state"])
            raise
        finally:
            if not conn.closed:
                conn.close()

        vs.state = SchemaState.ACTIVE
        vs.save(update_fields=["state"])

        logger.info(
            "Built view schema '%s' for workspace '%s' (%d tenants, %d tables)",
            view_schema_name,
            workspace.id,
            len(tenant_schemas),
            len(all_tables),
        )
        return vs

    def teardown_view_schema(self, view_schema: WorkspaceViewSchema) -> None:
        """Drop the physical PostgreSQL schema for a WorkspaceViewSchema."""
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(view_schema.schema_name)
                )
            )
            cursor.close()
        finally:
            conn.close()

    def _sanitize_schema_name(self, tenant_id: str) -> str:
        """Convert a tenant_id to a valid PostgreSQL schema name."""
        name = tenant_id.lower().replace("-", "_")
        name = "".join(c for c in name if c.isalnum() or c == "_")
        if name and name[0].isdigit():
            name = f"t_{name}"
        return name or "unknown"
