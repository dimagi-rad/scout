"""Operator command to merge duplicate User rows that share an email.

Usage:
    python manage.py merge_duplicate_users [--dry-run] [--email EMAIL]
                                           [--canonical-id ID] [--yes]
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models.functions import Lower

from apps.users.services.merge import MergeReport, merge_users, select_canonical

User = get_user_model()


class Command(BaseCommand):
    help = "Merge duplicate User rows that share an email address."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Print plan, write nothing.")
        parser.add_argument("--email", help="Only operate on the group sharing this email.")
        parser.add_argument(
            "--canonical-id", type=int,
            help="Force this user as canonical. Must be in the targeted group.",
        )
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    def handle(self, *args: Any, **opts: Any) -> None:
        groups = self._find_groups(target_email=opts.get("email"))
        if not groups:
            target = opts.get("email")
            self.stdout.write(
                f"no duplicates found{' for ' + target if target else ''}"
            )
            return

        plans: list[tuple[list[User], MergeReport, User]] = []
        for users in groups:
            canonical = self._pick_canonical(users, forced_id=opts.get("canonical_id"))
            for dup in users:
                if dup.pk == canonical.pk:
                    continue
                report = merge_users(canonical=canonical, duplicate=dup, dry_run=True)
                plans.append(([canonical, dup], report, canonical))
                self._print_plan(canonical, dup, report)

        if opts.get("dry_run"):
            return

        if not opts.get("yes"):
            response = input(
                f"About to merge {len(plans)} duplicate(s). Continue? [y/N] "
            ).strip().lower()
            if response != "y":
                self.stdout.write("aborted")
                return

        for users, _plan, canonical in plans:
            dup = next(u for u in users if u.pk != canonical.pk)
            dup_pk = dup.pk  # capture before merge_users() deletes the duplicate
            try:
                merge_users(canonical=canonical, duplicate=dup)
                self.stdout.write(f"merged User#{dup_pk} -> User#{canonical.pk}")
            except Exception as exc:  # noqa: BLE001 — best-effort per-group recovery
                self.stderr.write(f"failed User#{dup_pk} -> User#{canonical.pk}: {exc!r}")

    def _find_groups(self, *, target_email: str | None) -> list[list[User]]:
        qs = User.objects.exclude(email__isnull=True).exclude(email="")
        if target_email:
            qs = qs.filter(email__iexact=target_email)
        buckets: dict[str, list[User]] = defaultdict(list)
        for u in qs.annotate(lower_email=Lower("email")).order_by("created_at", "pk"):
            buckets[u.lower_email].append(u)
        return [group for group in buckets.values() if len(group) > 1]

    def _pick_canonical(self, users: list[User], *, forced_id: int | None) -> User:
        if forced_id is not None:
            forced = next((u for u in users if u.pk == forced_id), None)
            if forced is None:
                raise CommandError(
                    f"--canonical-id={forced_id} is not in the targeted duplicate group",
                )
            return forced
        return select_canonical(users)

    def _print_plan(self, canonical: User, duplicate: User, report: MergeReport) -> None:
        self.stdout.write(
            f"[merge] email='{canonical.email}'  canonical: User#{canonical.pk}  "
            f"duplicate: User#{duplicate.pk}"
        )
        self.stdout.write(
            f"  plan: socialaccounts={report.socialaccount_repointed} "
            f"emails(repoint/delete)={report.emailaddress_repointed}/"
            f"{report.emailaddress_deleted} "
            f"tenant(repoint/conflict)={report.tenant_membership_repointed}/"
            f"{report.tenant_membership_conflict_deleted} "
            f"workspace(repoint/conflict)={report.workspace_membership_repointed}/"
            f"{report.workspace_membership_conflict_merged}"
        )
