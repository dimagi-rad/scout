"""Restore memberships archived by an expired-token 401 that the provider still lists.

Before the verifier retried a 401 with a current credential, a 401 for an access
token it had just refreshed (or that a sibling had just rotated) was published as
CREDENTIAL_REJECTED, which archives every membership on the connection as if access
had been removed.

A candidate is an archived membership on an OAuth connection whose last denial is
still ``AUTH_TOKEN_EXPIRED`` and whose ``archived_at`` equals that denial's
``upstream_denied_at`` (the denial stamps both with one timestamp), so it was
archived by that denial and by nothing since. Nothing is restored from local state:
``--apply`` re-runs upstream verification for the candidates with the connection's
current credential, refreshing it if needed, and only a complete provider listing
un-archives the candidates it names. Rows archived for any other reason stay archived
even if listed. A dead credential, a provider outage or a second 401 restores
nothing, so a genuinely revoked user stays revoked. Idempotent.
"""

import logging
from collections import defaultdict

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.common.error_codes import ErrorCode
from apps.users.models import TenantConnection, TenantMembership
from apps.users.services.access_verification_providers import verify_provider
from apps.users.services.access_verification_service import verify_connection_access
from apps.users.services.access_verification_types import (
    AccessVerificationStatus,
    ProviderVerificationResult,
    VerificationOutcome,
)
from apps.users.services.oauth_scope import canonical_provider, memberships_on_provider

logger = logging.getLogger(__name__)


def _candidates(*, user_ids, connection_ids) -> dict:
    """``{(user_id, connection_id): {tenant_id, ...}}`` for archived token-expiry rows."""
    connections = TenantConnection.objects.filter(
        credential_type=TenantConnection.OAUTH,
        upstream_denial_code=ErrorCode.AUTH_TOKEN_EXPIRED,
        upstream_denied_at__isnull=False,
    )
    if user_ids:
        connections = connections.filter(user_id__in=user_ids)
    if connection_ids:
        connections = connections.filter(pk__in=connection_ids)
    grouped = defaultdict(set)
    for connection in connections.order_by("user_id", "pk"):
        archived = TenantMembership.all_objects.filter(
            user_id=connection.user_id,
            connection=connection,
            archived_at=connection.upstream_denied_at,
        )
        # The same rows a connection-wide denial archives and a claim accepts: one row
        # outside them would make the claim refuse the whole connection.
        if (
            canonical_provider(connection.provider) == "ocs"
            and connection.credential_type == TenantConnection.OAUTH
        ):
            archived = archived.filter(
                Q(provider_metadata__team_slug=connection.scope_key)
                | Q(provider_metadata__team_slug__isnull=True)
                | Q(provider_metadata__team_slug="")
            )
        for tenant_id in memberships_on_provider(archived, connection.provider, "tenant_id"):
            grouped[(connection.user_id, connection.pk)].add(tenant_id)
    return dict(grouped)


def _restricted_verifier(user_id, connection_id, candidate_ids):
    """``verify_provider`` whose complete listing names only candidates and live rows.

    Publication restores every archived row a complete listing names, whatever archived
    it; trimming the listing confines the restore to the candidates. Live rows stay in
    so a listed one is kept and an unlisted one is archived, exactly as without the trim.
    """

    async def verifier(snapshot, **kwargs):
        result = await verify_provider(snapshot, **kwargs)
        if result.outcome != VerificationOutcome.COMPLETE:
            return result
        keep = {
            external_id
            async for external_id in TenantMembership.all_objects.filter(
                Q(archived_at__isnull=True) | Q(tenant_id__in=candidate_ids),
                user_id=user_id,
                connection_id=connection_id,
            ).values_list("tenant__external_id", flat=True)
        }
        return ProviderVerificationResult.complete(result.external_ids & keep)

    return verifier


def _live(user_id, connection_id, tenant_ids) -> set:
    return set(
        TenantMembership.objects.filter(
            user_id=user_id, connection_id=connection_id, tenant_id__in=tenant_ids
        ).values_list("tenant_id", flat=True)
    )


class Command(BaseCommand):
    help = (
        "Restore memberships archived by an AUTH_TOKEN_EXPIRED 401 when the provider "
        "still lists them for the connection's current credential. Reports candidates; "
        "only --apply contacts providers and writes. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Re-verify upstream and restore listed tenants. Without it, only report.",
        )
        parser.add_argument(
            "--user-id", action="append", type=int, default=[], help="Repeatable filter."
        )
        parser.add_argument(
            "--connection-id", action="append", default=[], help="Repeatable filter."
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        self._log(f"restore_token_expired_memberships: {'APPLY' if apply else 'DRY RUN'}")
        candidates = _candidates(
            user_ids=options["user_id"], connection_ids=options["connection_id"]
        )
        total = sum(len(tenant_ids) for tenant_ids in candidates.values())
        self._log(f"Candidate memberships: {total} on {len(candidates)} connection(s)")
        for (user_id, connection_id), tenant_ids in candidates.items():
            self._log(f"  user {user_id} connection {connection_id}: {len(tenant_ids)}")
        if not apply:
            self._log("Dry run: nothing verified or written. Re-run with --apply.")
            return

        restored = 0
        for (user_id, connection_id), tenant_ids in candidates.items():
            # One connection at a time, and a failure on one must not stop the others.
            # async_to_sync because a command is a sync entry point; it keeps the
            # verifier's ORM work on this thread's connection.
            try:
                result = async_to_sync(verify_connection_access)(
                    user_id,
                    connection_id,
                    tenant_ids,
                    provider_verifier=_restricted_verifier(user_id, connection_id, tenant_ids),
                )
            except Exception:
                logger.exception("Re-verification failed for connection %s", connection_id)
                self._log(f"  connection {connection_id}: verification error, nothing restored")
                continue
            now_live = _live(user_id, connection_id, tenant_ids)
            restored += len(now_live)
            status = result.status.value
            if result.status != AccessVerificationStatus.VERIFIED and result.error_code:
                status = f"{status} ({result.error_code})"
            self._log(
                f"  connection {connection_id}: {status}; "
                f"restored {len(now_live)} of {len(tenant_ids)}"
            )
        self._log(f"Restored {restored} of {total} membership(s).")

    def _log(self, message: str) -> None:
        logger.info(message)
        self.stdout.write(message)
