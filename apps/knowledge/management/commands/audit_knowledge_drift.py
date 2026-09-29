"""List knowledge and learnings that reference names the current catalog no longer serves."""

import json
import uuid

from django.core.management.base import BaseCommand

from apps.knowledge.services.drift_audit import audit_knowledge_drift


class Command(BaseCommand):
    help = (
        "Read-only audit of knowledge entries, table knowledge and active learnings whose "
        "tables, columns, datasets or semantic members are missing from or hidden in the "
        "workspace's current semantic catalog. Makes no writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit a JSON array instead of readable text.",
        )
        parser.add_argument(
            "--workspace-id",
            action="append",
            type=uuid.UUID,
            dest="workspace_ids",
            help="Limit the audit to a workspace UUID. May be repeated.",
        )

    def handle(self, *args, **options):
        reports = audit_knowledge_drift(workspace_ids=options["workspace_ids"])
        found = {report.workspace_id for report in reports}
        for workspace_id in options["workspace_ids"] or []:
            if str(workspace_id) not in found:
                self.stderr.write(f"No workspace with id {workspace_id}.")
        if options["json"]:
            self.stdout.write(
                json.dumps([report.as_dict() for report in reports], indent=2, sort_keys=True)
            )
            return

        for report in reports:
            header = f"workspace={report.workspace_name!r} workspace_id={report.workspace_id}"
            if report.catalog_status != "active":
                self.stdout.write(f"[SKIPPED] {header}: no active semantic catalog to compare")
                continue
            status = "DRIFT" if report.drifted else "OK"
            self.stdout.write(
                f"[{status}] {header} catalog_version={report.catalog_version}: "
                f"{len(report.drifted)}/{report.checked} rows reference drifted names"
            )
            for row in report.drifted:
                self.stdout.write(f"  {row.source} {row.id} {row.label!r}")
                for reference in row.references:
                    self.stdout.write(
                        f"    {reference.kind} `{reference.name}` ({reference.reason})"
                    )
        drifted = sum(len(report.drifted) for report in reports)
        skipped = sum(report.catalog_status != "active" for report in reports)
        self.stdout.write(
            f"Summary: {drifted} drifted rows across {len(reports) - skipped} audited "
            f"workspaces; {skipped} skipped without an active catalog."
        )
