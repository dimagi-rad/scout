"""Archive legacy OCS memberships that record no team (#379).

Rows from before per-team OCS OAuth carry no ``team_slug``. The all-of coverage
gate counts each one as uncovered (``OCS_TEAM_MISSING``), and ``_sync_memberships``
never archives them, so they only block workspace access. Their owners get the
access back by reconnecting OCS once per team (#435).

Team-less rows with no connection at all are archived too: nothing can reach them
either. API-key memberships are left alone, because a key's experiments can
legitimately lack a team (see ``credential_coverage._membership_gap``).
"""

import logging
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.users.models import TenantConnection, TenantMembership, User
from apps.workspaces import access_cache
from apps.workspaces.access import all_of_access_enforced

logger = logging.getLogger(__name__)


def _teamless_ocs(memberships) -> tuple[list[TenantMembership], int]:
    """``(candidates, kept_api_key_rows)`` among the live rows of ``memberships``."""
    rows = (
        memberships.filter(archived_at__isnull=True, tenant__provider__startswith="ocs")
        .select_related("tenant", "connection")
        .order_by("user_id", "tenant_id")
    )
    candidates, kept = [], 0
    for membership in rows.iterator(chunk_size=2000):
        # A whitespace slug is no team either: account_scope strips, so no identity
        # can ever match it and the row is uncovered all the same.
        if str(membership.team_slug or "").strip():
            continue
        connection = membership.connection
        if connection is not None and connection.credential_type == TenantConnection.API_KEY:
            kept += 1
            continue
        candidates.append(membership)
    return candidates, kept


class Command(BaseCommand):
    help = (
        "Archive live OCS memberships that record no team (#379). Always reports counts "
        "per user id and per tenant; writes only with --apply, which requires the all-of "
        "access rule (WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT). Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Archive the memberships. Without it, only report what would change.",
        )
        parser.add_argument(
            "--user-id",
            action="append",
            type=int,
            default=[],
            help="Only consider this user (repeatable).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        # Under any-of access a team-less row still grants access and resolves a
        # credential, so archiving it would revoke access that works.
        if apply and not all_of_access_enforced():
            raise CommandError(
                "Refusing --apply: WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT is off, so "
                "team-less memberships still grant access."
            )
        self._log(f"archive_teamless_ocs_memberships: {'APPLY' if apply else 'DRY RUN'}")
        memberships = TenantMembership.all_objects.all()
        if options["user_id"]:
            memberships = memberships.filter(user_id__in=options["user_id"])
        candidates, kept = _teamless_ocs(memberships)
        by_user = Counter(m.user_id for m in candidates)
        by_tenant = Counter((m.tenant_id, m.tenant.external_id) for m in candidates)

        self._log(f"Team-less OCS memberships: {len(candidates)}")
        self._log("Per user id:")
        for user_id, count in sorted(by_user.items()):
            self._log(f"  user {user_id}: {count}")
        self._log("Per tenant:")
        for (tenant_id, external_id), count in sorted(
            by_tenant.items(), key=lambda item: str(item[0][0])
        ):
            self._log(f"  tenant {tenant_id} (ocs {external_id}): {count}")
        if kept:
            self._log(f"Kept {kept} team-less membership(s) on an API-key connection.")
        scanned = {m.pk for m in candidates}
        # Onboarding requires one live connection-backed membership, so these users
        # drop back to the onboarding page, which offers OCS to connect a team.
        kept_users = set(
            TenantMembership.objects.filter(user_id__in=list(by_user), connection__isnull=False)
            .exclude(pk__in=scanned)
            .values_list("user_id", flat=True)
        )
        stranded = sorted(set(by_user) - kept_users)
        if stranded:
            self._log("Users left with no live data source (they will see onboarding again):")
            for user_id in stranded:
                self._log(f"  user {user_id}")

        if not apply:
            self._log(f"Dry run: would archive {len(candidates)}. Re-run with --apply to write.")
            return

        per_user = [self._archive_for_user(user_id, scanned) for user_id in sorted(by_user)]
        self._log(f"Archived {sum(per_user)} membership(s) for {sum(map(bool, per_user))} user(s).")

    def _archive_for_user(self, user_id, scanned) -> int:
        with transaction.atomic():
            # Resolution writes memberships under this lock; re-select under it so a
            # sync that stamped a team since the scan keeps its row, and intersect with
            # the scan so nothing is archived that the report did not list.
            User.objects.select_for_update().filter(pk=user_id).first()
            candidates, _kept = _teamless_ocs(TenantMembership.all_objects.filter(user_id=user_id))
            archived = TenantMembership.all_objects.filter(
                pk__in=[m.pk for m in candidates if m.pk in scanned], archived_at__isnull=True
            ).update(archived_at=timezone.now())
            # A no-op outside a request scope; kept so the write stays safe if it is
            # ever reused from one, like the other archival paths.
            transaction.on_commit(lambda: access_cache.invalidate(user_id=user_id))
        return archived

    def _log(self, message: str) -> None:
        logger.info(message)
        self.stdout.write(message)
