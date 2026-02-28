"""Management command to purge all materialized tenant data from the dev environment."""

import logging

from django.core.management.base import BaseCommand

from apps.projects.models import TenantMetadata, TenantSchema, TenantWorkspace
from apps.projects.services.schema_manager import SchemaManager

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Purge all materialized tenant data: drops managed-DB schemas, "
        "deletes TenantSchema/MaterializationRun/TenantMetadata records, "
        "and clears data dictionaries. Chat, artifacts, and learnings are preserved."
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
        workspace_count = TenantWorkspace.objects.exclude(data_dictionary=None).count()

        self.stdout.write(
            self.style.WARNING(
                f"\nPurge synced data summary:\n"
                f"  TenantSchema records (+ cascaded MaterializationRun): {schema_count}\n"
                f"  TenantMetadata records: {metadata_count}\n"
                f"  TenantWorkspace records with data_dictionary to clear: {workspace_count}\n"
            )
        )

        if not options["confirm"]:
            self.stdout.write("Dry run — nothing deleted. Re-run with --confirm to proceed.\n")
            raise SystemExit(0)

        manager = SchemaManager()
        teardown_errors = []

        for tenant_schema in TenantSchema.objects.all():
            try:
                manager.teardown(tenant_schema)
                self.stdout.write(f"  Dropped schema: {tenant_schema.schema_name}")
            except Exception as exc:
                teardown_errors.append((tenant_schema.schema_name, str(exc)))
                self.stdout.write(
                    self.style.ERROR(f"  Failed to drop schema {tenant_schema.schema_name}: {exc}")
                )

        deleted_schemas, _ = TenantSchema.objects.all().delete()
        deleted_metadata, _ = TenantMetadata.objects.all().delete()
        TenantWorkspace.objects.update(data_dictionary=None, data_dictionary_generated_at=None)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone.\n"
                f"  Deleted {deleted_schemas} TenantSchema/MaterializationRun rows\n"
                f"  Deleted {deleted_metadata} TenantMetadata rows\n"
                f"  Cleared data_dictionary on {workspace_count} TenantWorkspace(s)\n"
            )
        )

        if teardown_errors:
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(teardown_errors)} schema teardown error(s) — "
                    "DB records were still deleted.\n"
                )
            )
