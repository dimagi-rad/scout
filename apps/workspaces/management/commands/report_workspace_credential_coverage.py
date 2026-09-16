"""Report local credential readiness for every workspace member."""

import json

from django.core.management.base import BaseCommand

from apps.workspaces.services.credential_coverage import get_workspace_credential_coverage


class Command(BaseCommand):
    help = (
        "Read-only audit of local tenant-credential readiness for workspace members. "
        "This does not contact providers or confirm live upstream access."
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
            dest="workspace_ids",
            help="Limit the report to a workspace UUID. May be repeated.",
        )
        parser.add_argument(
            "--user-id",
            action="append",
            type=int,
            dest="user_ids",
            help="Limit the report to a user ID. May be repeated.",
        )

    def handle(self, *args, **options):
        reports = get_workspace_credential_coverage(
            workspace_ids=options["workspace_ids"],
            user_ids=options["user_ids"],
        )
        if options["json"]:
            self.stdout.write(
                json.dumps([report.as_dict() for report in reports], indent=2, sort_keys=True)
            )
            return

        self.stdout.write(
            "Local credential readiness only; this report performs no live upstream confirmation."
        )
        covered = 0
        for report in reports:
            status = "COVERED" if report.covered else "GAPS"
            self.stdout.write(
                f"[{status}] workspace={report.workspace_name!r} "
                f"workspace_id={report.workspace_id} user_id={report.user_id}"
            )
            if report.covered:
                covered += 1
                continue
            for gap in report.gaps:
                team = (
                    f" team={gap.team_slug or gap.team_name!r}"
                    if gap.team_slug or gap.team_name
                    else ""
                )
                self.stdout.write(
                    f"  {gap.code}: tenant={gap.tenant_name!r} "
                    f"tenant_id={gap.tenant_id} provider={gap.provider}{team}"
                )
        self.stdout.write(
            f"Summary: {covered}/{len(reports)} workspace-member rows locally ready; "
            f"{len(reports) - covered} with gaps."
        )
