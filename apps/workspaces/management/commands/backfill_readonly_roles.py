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
        "Idempotent — safe to run multiple times. Each schema is handled "
        "independently so a single drift (a Django row whose physical schema was "
        "dropped) does not abort the rest of the backfill."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be backfilled without issuing any GRANT/CREATE statements.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be made."))

        conn = _schema_manager.get_managed_db_connection()
        cursor = conn.cursor()
        mgr = SchemaManager()

        ok = 0
        failed = 0
        try:
            # Backfill tenant schemas. MATERIALIZING is a dead state (the live
            # pipeline never persists it), so only ACTIVE rows are selected.
            tenant_schemas = TenantSchema.objects.filter(state=SchemaState.ACTIVE)
            for ts in tenant_schemas:
                # Per-schema isolation: drift (physical schema missing) on one row
                # must not strand every later schema's _ro role (fail-closed outage).
                try:
                    if not dry_run:
                        self._backfill_schema(cursor, mgr, ts.schema_name)
                    self.stdout.write(f"  Backfilled role for schema: {ts.schema_name}")
                    ok += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "backfill_readonly_roles: failed for tenant schema '%s'; continuing",
                        ts.schema_name,
                    )
                    self.stderr.write(
                        self.style.ERROR(f"  SKIP (error) tenant schema: {ts.schema_name}")
                    )

            # Backfill view schemas
            view_schemas = WorkspaceViewSchema.objects.filter(
                state=SchemaState.ACTIVE,
            ).select_related("workspace")
            for vs in view_schemas:
                try:
                    if not dry_run:
                        self._backfill_view_schema(cursor, mgr, vs)
                    self.stdout.write(f"  Backfilled role for view schema: {vs.schema_name}")
                    ok += 1
                except Exception:
                    failed += 1
                    logger.exception(
                        "backfill_readonly_roles: failed for view schema '%s'; continuing",
                        vs.schema_name,
                    )
                    self.stderr.write(
                        self.style.ERROR(f"  SKIP (error) view schema: {vs.schema_name}")
                    )

            summary = f"Done. {ok} backfilled, {failed} skipped."
            self.stdout.write(
                self.style.SUCCESS(summary) if not failed else self.style.WARNING(summary)
            )
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

    def _backfill_view_schema(self, cursor, mgr, vs) -> None:
        """Create the view-schema role and grant it access to constituent tenant schemas.

        Mirrors build_view_schema: the role gets SELECT on existing tables in each
        constituent tenant schema AND default privileges, so a rematerialization
        that runs between view rebuilds doesn't leave the new tables unreadable.
        """
        self._backfill_schema(cursor, mgr, vs.schema_name)
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
            # Default privileges so tables created later (by a rematerialization
            # before the view is rebuilt) are still readable through this role.
            cursor.execute(
                psycopg.sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA {} "
                    "GRANT SELECT ON TABLES TO {}"
                ).format(
                    psycopg.sql.Identifier(ts.schema_name),
                    psycopg.sql.Identifier(role),
                )
            )
