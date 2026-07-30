"""Physically purge soft-deleted artifacts past their retention window.

Artifact deletion is soft (``is_deleted``) with an undelete endpoint, so rows
are never freed (arch #254, finding 09#9 — "soft-delete never frees rows").
This command physically deletes artifacts soft-deleted longer ago than the
retention window, reclaiming the storage while keeping a grace period for
undelete. Run it on a schedule (e.g. nightly).
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.artifacts.models import Artifact

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30


class Command(BaseCommand):
    help = (
        "Physically delete artifacts that were soft-deleted more than "
        "--retention-days ago (default 30). Without --confirm, prints a "
        "dry-run summary only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--retention-days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help="Purge artifacts soft-deleted more than this many days ago.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            default=False,
            help="Actually delete. Without this flag, only a summary is printed.",
        )

    def handle(self, *args, **options):
        retention_days = options["retention_days"]
        cutoff = timezone.now() - timedelta(days=retention_days)

        # all_objects bypasses the SoftDeleteManager so we can see deleted rows.
        qs = Artifact.all_objects.filter(is_deleted=True, deleted_at__lt=cutoff)
        count = qs.count()

        self.stdout.write(
            self.style.WARNING(f"Soft-deleted artifacts older than {retention_days} days: {count}")
        )

        if not options["confirm"]:
            self.stdout.write("Dry run — pass --confirm to delete.")
            return

        deleted, _ = qs.delete()
        logger.info("Purged %d soft-deleted artifacts (retention %dd)", count, retention_days)
        self.stdout.write(self.style.SUCCESS(f"Purged {deleted} artifact rows."))
