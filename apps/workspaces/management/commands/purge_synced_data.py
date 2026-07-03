"""Management command to purge all materialized tenant data from the dev environment."""

import logging

from django.core.management.base import BaseCommand

from apps.workspaces.models import (
    TenantMetadata,
    TenantSchema,
    Workspace,
    WorkspaceViewSchema,
)
from apps.workspaces.services.schema_manager import SchemaManager

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Purge all materialized tenant data: drops managed-DB tenant AND view "
        "schemas, deletes TenantSchema/MaterializationRun/TenantMetadata/"
        "WorkspaceViewSchema records, and clears data dictionaries. Chat, "
        "artifacts, and learnings are preserved."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            default=False,
            help="Actually perform the deletion. Without this flag a dry-run summary is printed.",
        )

    def handle(self, *args, **options):
        schema_count = TenantSchema.objects.count()
        metadata_count = TenantMetadata.objects.count()
        view_schema_count = WorkspaceViewSchema.objects.count()
        workspace_count = Workspace.objects.exclude(data_dictionary=None).count()

        self.stdout.write(
            self.style.WARNING(
                f"\nPurge synced data summary:\n"
                f"  TenantSchema records (+ cascaded MaterializationRun): {schema_count}\n"
                f"  WorkspaceViewSchema records (+ ws_* schemas): {view_schema_count}\n"
                f"  TenantMetadata records: {metadata_count}\n"
                f"  Workspace records with data_dictionary to clear: {workspace_count}\n"
            )
        )

        if not options["confirm"]:
            self.stdout.write("Dry run — nothing deleted. Re-run with --confirm to proceed.\n")
            raise SystemExit(0)

        manager = SchemaManager()
        teardown_errors = []

        # Drop the multi-tenant view schemas first. Their ws_* physical schemas are
        # NOT reached by the tenant DROP ... CASCADE below (that only empties the
        # namespaced views), so without this the ws_* schemas + WorkspaceViewSchema
        # rows survive as orphans (rows typically still ACTIVE over hollow schemas)
        # — arch #255, 09#4.
        for view_schema in WorkspaceViewSchema.objects.all():
            try:
                manager.teardown_view_schema(view_schema)
                self.stdout.write(f"  Dropped view schema: {view_schema.schema_name}")
            except Exception as exc:
                teardown_errors.append((view_schema.schema_name, str(exc)))
                self.stdout.write(
                    self.style.ERROR(
                        f"  Failed to drop view schema {view_schema.schema_name}: {exc}"
                    )
                )

        for tenant_schema in TenantSchema.objects.all():
            try:
                manager.teardown(tenant_schema)
                self.stdout.write(f"  Dropped schema: {tenant_schema.schema_name}")
            except Exception as exc:
                teardown_errors.append((tenant_schema.schema_name, str(exc)))
                self.stdout.write(
                    self.style.ERROR(f"  Failed to drop schema {tenant_schema.schema_name}: {exc}")
                )

        deleted_view_schemas, _ = WorkspaceViewSchema.objects.all().delete()
        deleted_schemas, _ = TenantSchema.objects.all().delete()
        deleted_metadata, _ = TenantMetadata.objects.all().delete()
        Workspace.objects.update(data_dictionary=None, data_dictionary_generated_at=None)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone.\n"
                f"  Deleted {deleted_schemas} TenantSchema/MaterializationRun rows\n"
                f"  Deleted {deleted_view_schemas} WorkspaceViewSchema rows\n"
                f"  Deleted {deleted_metadata} TenantMetadata rows\n"
                f"  Cleared data_dictionary on {workspace_count} Workspace(s)\n"
            )
        )

        if teardown_errors:
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(teardown_errors)} schema teardown error(s) — "
                    "DB records were still deleted.\n"
                )
            )
