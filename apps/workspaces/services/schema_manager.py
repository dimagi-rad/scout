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

from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema

logger = logging.getLogger(__name__)


def readonly_role_name(schema_name: str) -> str:
    """Derive the read-only PostgreSQL role name for a schema."""
    return f"{schema_name}_ro"


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
            # Ensure the physical schema still exists — it may have been
            # dropped externally while the Django record remained ACTIVE.
            self._ensure_physical_schema(schema_name)
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
                self._create_readonly_role(cursor, schema_name)
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

    def _ensure_physical_schema(self, schema_name: str) -> None:
        """Ensure the physical PostgreSQL schema and readonly role exist.

        Idempotent — safe to call on every provision(). Handles the case where
        the physical schema was dropped externally but the Django record remains.
        """
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    psycopg.sql.Identifier(schema_name)
                )
            )
            self._create_readonly_role(cursor, schema_name)
            cursor.close()
        finally:
            conn.close()

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
            self._create_readonly_role(cursor, tenant_schema.schema_name)
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
            self._drop_readonly_role(cursor, tenant_schema.schema_name)
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

            # Step 2+3: Create per-tenant namespaced views
            from apps.users.models import Tenant

            # Pre-compute prefixes and detect collisions before creating any views
            prefix_to_tenant: dict[str, str] = {}
            tenant_prefixes: list[tuple[str, str, str]] = (
                []
            )  # (schema_name, tenant_external_id, prefix)
            for schema_name, tenant_external_id in tenant_schemas:
                tenant_obj = Tenant.objects.get(external_id=tenant_external_id)
                prefix = self._sanitize_schema_name(tenant_obj.canonical_name)
                if prefix in prefix_to_tenant:
                    raise ValueError(
                        f"Canonical name collision: tenants '{prefix_to_tenant[prefix]}' and "
                        f"'{tenant_external_id}' both sanitize to prefix '{prefix}'"
                    )
                prefix_to_tenant[prefix] = tenant_external_id
                tenant_prefixes.append((schema_name, tenant_external_id, prefix))

            # Collect all (view_name, schema_name, table_name) and detect full
            # view name collisions before executing any DDL. This catches cases
            # where the __ delimiter is ambiguous (e.g. prefix "foo__bar" + table
            # "baz" vs prefix "foo" + table "bar__baz").
            planned_views: list[tuple[str, str, str]] = []
            seen_view_names: dict[str, str] = {}  # view_name → tenant_external_id
            for schema_name, tenant_external_id, prefix in tenant_prefixes:
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')",
                    (schema_name,),
                )
                for (table_name,) in cursor.fetchall():
                    view_name = f"{prefix}__{table_name}"
                    if view_name in seen_view_names:
                        raise ValueError(
                            f"View name collision: '{view_name}' produced by both "
                            f"tenant '{seen_view_names[view_name]}' and '{tenant_external_id}'"
                        )
                    seen_view_names[view_name] = tenant_external_id
                    planned_views.append((view_name, schema_name, table_name))

            for view_name, schema_name, table_name in planned_views:
                cursor.execute(
                    psycopg.sql.SQL(
                        "CREATE OR REPLACE VIEW {}.{} AS SELECT * FROM {}.{}"
                    ).format(
                        psycopg.sql.Identifier(view_schema_name),
                        psycopg.sql.Identifier(view_name),
                        psycopg.sql.Identifier(schema_name),
                        psycopg.sql.Identifier(table_name),
                    )
                )
            views_created = len(planned_views)

            # Create read-only role for the view schema
            self._create_readonly_role(cursor, view_schema_name)

            # Grant read access to each constituent tenant schema
            # (views reference tables in these schemas directly)
            view_role = readonly_role_name(view_schema_name)
            for tenant_schema_name, _ in tenant_schemas:
                cursor.execute(
                    psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        psycopg.sql.Identifier(tenant_schema_name),
                        psycopg.sql.Identifier(view_role),
                    )
                )
                cursor.execute(
                    psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                        psycopg.sql.Identifier(tenant_schema_name),
                        psycopg.sql.Identifier(view_role),
                    )
                )

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
            "Built view schema '%s' for workspace '%s' (%d tenants, %d views)",
            view_schema_name,
            workspace.id,
            len(tenant_schemas),
            views_created,
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
            self._drop_readonly_role(cursor, view_schema.schema_name)
            cursor.close()
        finally:
            conn.close()

    def _drop_readonly_role(self, cursor, schema_name: str) -> None:
        """Drop the read-only PostgreSQL role for a schema.

        Issues DROP OWNED BY first to revoke all privileges the role holds
        (including cross-schema grants from view schema roles), then drops
        the role itself.
        """
        role_name = readonly_role_name(schema_name)
        # Check if role exists before DROP OWNED BY (which errors on missing roles)
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if not cursor.fetchone():
            return
        # DROP OWNED BY revokes all privileges granted TO this role (e.g. USAGE
        # and SELECT on constituent tenant schemas for view schema roles). It does
        # NOT drop or modify the tenant schemas themselves — only the grants that
        # this specific role holds. Tenant schemas and their own _ro roles are
        # unaffected. This is required because PostgreSQL refuses to DROP ROLE
        # while the role still holds any privileges.
        cursor.execute(
            psycopg.sql.SQL("DROP OWNED BY {}").format(psycopg.sql.Identifier(role_name))
        )
        cursor.execute(
            psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role_name))
        )

    def _create_readonly_role(self, cursor, schema_name: str) -> None:
        """Create a read-only PostgreSQL role for a schema.

        Idempotent — checks pg_roles before creating. Grants USAGE on the
        schema and sets ALTER DEFAULT PRIVILEGES so tables created later by
        the materializer are automatically readable.
        """
        role_name = readonly_role_name(schema_name)
        # Idempotent role creation — pg doesn't have CREATE ROLE IF NOT EXISTS
        cursor.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (role_name,),
        )
        if not cursor.fetchone():
            try:
                cursor.execute(
                    psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(
                        psycopg.sql.Identifier(role_name)
                    )
                )
            except psycopg.errors.DuplicateObject:
                pass  # Race condition: another process created it between check and create
        cursor.execute(
            psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )
        cursor.execute(
            psycopg.sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO {}"
            ).format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role_name),
            )
        )

    def _sanitize_schema_name(self, tenant_id: str) -> str:
        """Convert a tenant_id to a valid PostgreSQL schema name."""
        name = tenant_id.lower().replace("-", "_")
        name = "".join(c for c in name if c.isalnum() or c == "_")
        if name and name[0].isdigit():
            name = f"t_{name}"
        return name or "unknown"
