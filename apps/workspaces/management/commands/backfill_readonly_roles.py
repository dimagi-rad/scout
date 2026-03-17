"""Management command to backfill read-only PostgreSQL roles for existing schemas."""

import logging

import psycopg.sql
from django.core.management.base import BaseCommand

from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceViewSchema
from apps.workspaces.services import schema_manager as _schema_manager
from apps.workspaces.services.schema_manager import (
    SchemaManager,
    readonly_role_name,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Create read-only PostgreSQL roles for all active tenant and view schemas. "
        "Idempotent — safe to run multiple times."
    )

    def handle(self, *args, **options):
        conn = _schema_manager.get_managed_db_connection()
        cursor = conn.cursor()
        mgr = SchemaManager()

        try:
            # Backfill tenant schemas
            tenant_schemas = TenantSchema.objects.filter(
                state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
            )
            for ts in tenant_schemas:
                self._backfill_schema(cursor, mgr, ts.schema_name)
                self.stdout.write(f"  Backfilled role for schema: {ts.schema_name}")

            # Backfill view schemas
            view_schemas = WorkspaceViewSchema.objects.filter(
                state=SchemaState.ACTIVE,
            ).select_related("workspace")
            for vs in view_schemas:
                self._backfill_schema(cursor, mgr, vs.schema_name)
                # Grant access to constituent tenant schemas
                role = readonly_role_name(vs.schema_name)
                tenant_schemas_for_ws = TenantSchema.objects.filter(
                    tenant__in=vs.workspace.tenants.all(),
                    state=SchemaState.ACTIVE,
                )
                for ts in tenant_schemas_for_ws:
                    cursor.execute(
                        psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                            psycopg.sql.Identifier(ts.schema_name),
                            psycopg.sql.Identifier(role),
                        )
                    )
                    cursor.execute(
                        psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                            psycopg.sql.Identifier(ts.schema_name),
                            psycopg.sql.Identifier(role),
                        )
                    )
                self.stdout.write(f"  Backfilled role for view schema: {vs.schema_name}")

            self.stdout.write(self.style.SUCCESS("Done."))
        finally:
            cursor.close()
            conn.close()

    def _backfill_schema(self, cursor, mgr, schema_name: str) -> None:
        """Create role and grants for a single schema."""
        mgr._create_readonly_role(cursor, schema_name)
        # Also grant on existing tables (ALTER DEFAULT PRIVILEGES only covers future tables)
        role = readonly_role_name(schema_name)
        cursor.execute(
            psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                psycopg.sql.Identifier(schema_name),
                psycopg.sql.Identifier(role),
            )
        )
