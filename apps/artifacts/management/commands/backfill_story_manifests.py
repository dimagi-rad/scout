"""Persist semantic-query manifests for stories saved before manifests existed.

Read paths derive a missing manifest in memory but no longer save it (#593), so a
legacy story keeps ``semantic_queries`` empty in the database and the artifact
list shows it without the live-data badge until someone writes to it.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.artifacts.models import Artifact, ArtifactType
from apps.artifacts.services.graph_manifest import backfill_missing_semantic_query_manifest

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Persist the semantic-query manifest of every live story that has none. "
        "Prints what it would do unless --apply is given. Idempotent and safe to "
        "run alongside the app: each story is re-checked under its row lock."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the manifests. Without it, only list the stories that need one.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        candidates = Artifact.objects.filter(
            artifact_type=ArtifactType.STORY,
            semantic_queries=[],
            semantic_query_manifest={},
        ).order_by("pk")
        artifact_ids = list(candidates.values_list("pk", flat=True))

        if not apply:
            for artifact_id in artifact_ids:
                self.stdout.write(f"  Would backfill story {artifact_id}")
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {len(artifact_ids)} stories need a manifest. "
                    "Re-run with --apply to write them."
                )
            )
            return

        done = 0
        failed = 0
        for artifact_id in artifact_ids:
            # Re-fetched one at a time: a story deleted since the listing is skipped.
            artifact = Artifact.objects.filter(pk=artifact_id).first()
            if artifact is None:
                continue
            try:
                backfill_missing_semantic_query_manifest(artifact)
            except Exception:
                failed += 1
                logger.exception("backfill_story_manifests: failed for story %s", artifact_id)
                self.stderr.write(self.style.ERROR(f"  Failed story {artifact_id}"))
                continue
            done += 1
            self.stdout.write(f"  Backfilled story {artifact_id}")

        summary = f"Backfilled {done} stories, {failed} failed."
        if failed:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
