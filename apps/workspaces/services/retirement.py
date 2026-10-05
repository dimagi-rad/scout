"""Retiring tenant schemas, view schemas and abandoned load candidates.

The registered tasks in ``apps.workspaces.tasks`` are thin adapters over these.
Re-queues and follow-up work go through ``task_dispatch`` by registered name,
so this module never imports the tasks module.
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

import psycopg
import psycopg.errors
from django.conf import settings
from django.db import InterfaceError as DjangoInterfaceError
from django.db import OperationalError as DjangoOperationalError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from procrastinate.exceptions import AlreadyEnqueued

from apps.users.models import Tenant
from apps.workspaces.models import (
    VIEW_SCHEMA_CASCADE_TEARDOWN_ERROR,
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceViewSchema,
)
from apps.workspaces.services import publication
from apps.workspaces.services.data_operation import (
    DataLockTimeout,
    run_data_thread,
    tenant_data_lock_if_free,
)
from apps.workspaces.services.data_operation import (
    to_thread_fresh_db as _to_thread_fresh_db,
)
from apps.workspaces.services.load_candidates import (
    Promotion,
    abandoned_workspace_candidates,
    candidate_last_attempt_at,
    promote_candidate_schema,
    settle_orphaned_workspace_candidates,
    unresumable_workspace_candidates,
)
from apps.workspaces.services.refresh_requests import (
    LegacyRefreshReconciliation,
    fail_claimed_refresh_candidate,
    find_legacy_refresh_jobs,
    settle_finished_refresh_candidates,
)
from apps.workspaces.services.schema_manager import SchemaManager, SchemaStillReferenced
from apps.workspaces.task_dispatch import (
    adefer_rebuild_workspace_view_schema,
    adefer_teardown_schema,
    adefer_teardown_view_schema,
    configure_drop_abandoned_candidate,
    configure_teardown_schema,
)

logger = logging.getLogger(__name__)


# Old ACTIVE schemas are retired after a delay so in-flight queries can drain;
# retirement itself then waits for dependent sibling views to move.
_RETIRE_AFTER = timedelta(minutes=30)


def promote_and_queue_retirement(candidate_id, **promotion) -> Promotion:
    """Promote a candidate and queue the demoted schemas' teardown in one commit.

    Nothing sweeps a TEARDOWN row whose teardown was never queued, so a worker
    dying between the two must not be able to strand the old schema.
    """
    with transaction.atomic():
        outcome = promote_candidate_schema(candidate_id, **promotion)
        for schema_id in outcome.retired_schema_ids:
            configure_teardown_schema(
                schedule_in={"seconds": int(_RETIRE_AFTER.total_seconds())},
            ).defer(schema_id=str(schema_id))
    return outcome


async def defer_abandoned_candidate_drops(tenant_id, *, keep_id) -> None:
    await _to_thread_fresh_db(_abandon_and_queue_drops, tenant_id, keep_id)


def _abandon_and_queue_drops(tenant_id, keep_id) -> None:
    """Take abandoned candidates out of resume and queue their drops in one commit.

    Clearing the resume evidence without a queued drop would strand the schema:
    nothing else would ever drop a FAILED candidate no load can resume.
    """
    with transaction.atomic():
        for schema in abandoned_workspace_candidates(tenant_id, keep_id=keep_id):
            # Delayed: this writer holds T until its whole load publishes, and the
            # drop needs T, so an immediate job would only find it busy.
            _queue_candidate_drop_sync(schema, delay=_CANDIDATE_DROP_DELAY_SECONDS)


def _drop_lock(schema_id) -> str:
    return f"drop_abandoned_candidate:{schema_id}"


def _drop_job(schema, delay: int):
    """One drop per candidate, pinned to the attempt it was judged on."""
    schedule = {"schedule_in": {"seconds": delay}} if delay else {}
    job = configure_drop_abandoned_candidate(queueing_lock=_drop_lock(schema.id), **schedule)
    args = {
        "schema_id": str(schema.id),
        "last_attempt_at": schema.last_attempt_at.isoformat(),
        "load_job_id": schema.load_job_id,
    }
    return job, args


async def _queue_candidate_drop(schema, *, delay: int = 0) -> None:
    job, args = _drop_job(schema, delay)
    await job.defer_async(**args)


def _queue_candidate_drop_sync(schema, *, delay: int = 0) -> None:
    """Queue inside the caller's transaction; an already-queued drop covers it."""
    job, args = _drop_job(schema, delay)
    try:
        # Savepoint: the queueing-lock violation must not abort the outer commit.
        with transaction.atomic():
            job.defer(**args)
    except AlreadyEnqueued:
        return


_CANDIDATE_DROP_DELAY_SECONDS = 15 * 60


class _TenantBusy(Exception):
    """A writer holds the tenant's T; the drop is retried later rather than waiting."""


_CANDIDATE_DROP_RETRYABLE = (
    DataLockTimeout,
    psycopg.OperationalError,
    psycopg.InterfaceError,
    DjangoOperationalError,
    DjangoInterfaceError,
)


_CANDIDATE_DROP_RETRY_BASE_SECONDS = 60


_CANDIDATE_DROP_RETRY_MAX_SECONDS = 3600


_CANDIDATE_DROP_MAX_ATTEMPTS = 10


_CANDIDATE_DROP_BUSY_WARNING_INTERVAL = 96  # One day of 15-minute busy checks.


async def drop_abandoned_candidate(
    schema_id: str,
    attempt: int = 0,
    last_attempt_at: str = "",
    load_job_id: int | None = None,
    busy_count: int = 0,
) -> None:
    """Drop the partial data of a failed candidate no load will resume.

    Holds T so a writer cannot be resuming this candidate while it is dropped,
    but never waits for it: a load can hold T for hours, so a busy tenant is
    simply re-queued. ``last_attempt_at`` and ``load_job_id`` pin the attempt
    the candidate was judged abandoned on; if a load has resumed it since (a
    resume rewrites the owner, and a new run moves the attempt time), it is the
    pending generation's resume point and is kept. A failed drop is retried
    with backoff; after the last attempt the next sweep queues a fresh one.
    """
    schema = await TenantSchema.objects.filter(id=schema_id).afirst()
    if schema is None:
        return
    try:
        async with tenant_data_lock_if_free(schema.tenant_id) as locked:
            if not locked:
                raise _TenantBusy(f"tenant {schema.tenant_id} is being loaded")
            await schema.arefresh_from_db()
            if schema.state != SchemaState.FAILED or schema.load_workspace_id is None:
                return
            if load_job_id is not None and schema.load_job_id != load_job_id:
                return
            # Run reconciliation also moves this pin; the next sweep re-judges it.
            if last_attempt_at:
                current = await _to_thread_fresh_db(candidate_last_attempt_at, schema.id)
                if current is None or current != datetime.fromisoformat(last_attempt_at):
                    return
            await run_data_thread(SchemaManager().teardown, schema)
            await (
                MaterializationRun.objects.filter(tenant_schema=schema)
                .exclude(state=MaterializationRun.RunState.STALE)
                .aupdate(state=MaterializationRun.RunState.STALE)
            )
            schema.state = SchemaState.EXPIRED
            await schema.asave(update_fields=["state"])
    except TenantSchema.DoesNotExist:
        return
    except _TenantBusy:
        # Normal while a load runs: re-queue at a fixed delay without spending
        # the retry budget, which is for drops that actually failed.
        busy_count += 1
        if busy_count % _CANDIDATE_DROP_BUSY_WARNING_INTERVAL == 0:
            logger.warning(
                "Abandoned candidate %s still blocked by tenant %s after %d consecutive busy checks",
                schema_id,
                schema.tenant_id,
                busy_count,
            )
        await _requeue_candidate_drop(
            schema_id,
            _CANDIDATE_DROP_DELAY_SECONDS,
            attempt,
            last_attempt_at,
            load_job_id,
            busy_count=busy_count,
        )
    except _CANDIDATE_DROP_RETRYABLE as exc:
        # A query bug (ProgrammingError and friends) is deliberately absent: it
        # must surface, not be retried as contention.
        if attempt + 1 >= _CANDIDATE_DROP_MAX_ATTEMPTS:
            logger.exception(
                "Giving up dropping abandoned candidate %s after attempt %d; the next "
                "sweep queues it again",
                schema_id,
                attempt + 1,
            )
            return
        logger.warning(
            "Dropping abandoned candidate %s failed (attempt %d): %s", schema_id, attempt + 1, exc
        )
        delay = min(
            _CANDIDATE_DROP_RETRY_BASE_SECONDS * (2**attempt), _CANDIDATE_DROP_RETRY_MAX_SECONDS
        )
        await _requeue_candidate_drop(schema_id, delay, attempt + 1, last_attempt_at, load_job_id)


async def _requeue_candidate_drop(
    schema_id, delay, attempt, last_attempt_at, load_job_id, *, busy_count=0
):
    # The running job holds no queueing lock once doing, so the re-queue can take
    # it; a drop the sweep queued meanwhile already covers this one.
    try:
        await configure_drop_abandoned_candidate(
            schedule_in={"seconds": delay}, queueing_lock=_drop_lock(schema_id)
        ).defer_async(
            schema_id=str(schema_id),
            attempt=attempt,
            last_attempt_at=last_attempt_at,
            load_job_id=load_job_id,
            busy_count=busy_count,
        )
    except AlreadyEnqueued:
        return


# How long a failed candidate of the still-pending generation waits for a retry
# to resume it before the sweep treats it as abandoned.
_UNRESUMED_CANDIDATE_TTL = timedelta(hours=24)


async def sweep_workspace_load_candidates() -> dict:
    """Reclaim workspace-load candidates whose writer died or no load will resume.

    A load settles orphans only when the same tenant loads again, so a tenant
    that is never reloaded would otherwise keep a dead candidate's schema
    forever. A tenant whose T is held has a live writer and is skipped.
    """
    tenant_ids = [
        tenant_id
        async for tenant_id in TenantSchema.objects.filter(
            load_workspace_id__isnull=False,
            state__in=[SchemaState.PROVISIONING, SchemaState.FAILED],
        )
        .values_list("tenant_id", flat=True)
        .distinct()
    ]
    counts = {"settled": 0, "drops_queued": 0, "skipped_busy": 0}
    for tenant_id in tenant_ids:
        try:
            async with tenant_data_lock_if_free(tenant_id) as locked:
                if not locked:
                    counts["skipped_busy"] += 1
                    continue
                orphans = await _to_thread_fresh_db(settle_orphaned_workspace_candidates, tenant_id)
                for orphan in orphans:
                    logger.error(
                        "Settled orphaned load candidate %s (%s) for tenant %s: its writer died",
                        orphan.id,
                        orphan.schema_name,
                        tenant_id,
                    )
                counts["settled"] += len(orphans)
                abandoned = await _to_thread_fresh_db(
                    unresumable_workspace_candidates,
                    tenant_id,
                    stale_before=timezone.now() - _UNRESUMED_CANDIDATE_TTL,
                )
        except Exception:
            logger.exception("sweep_workspace_load_candidates: tenant %s failed", tenant_id)
            continue
        for schema in abandoned:
            try:
                await _queue_candidate_drop(schema)
            except AlreadyEnqueued:
                continue
            except Exception:
                logger.exception("Failed to queue cleanup of candidate '%s'", schema.schema_name)
                continue
            counts["drops_queued"] += 1
    return counts


async def drop_claimed_refresh_schema_and_fail(schema, job_id: int) -> None:
    """Fail and drop only a candidate still owned by this refresh job."""
    claimed = await _to_thread_fresh_db(fail_claimed_refresh_candidate, schema.id, job_id)
    if claimed is None:
        # Someone else settled this claimed candidate (e.g. the reconciler after a
        # false stall); its queued drop may have run before this job's last write.
        try:
            await drop_failed_refresh_schema(schema.id)
        except Exception:
            logger.exception("Failed to drop settled refresh schema '%s'", schema.schema_name)
        return
    manager = SchemaManager()
    try:
        await asyncio.to_thread(manager.teardown, claimed)
    except Exception:
        logger.exception("Failed to drop schema '%s' during cleanup", claimed.schema_name)


async def drop_failed_refresh_schema(schema_id) -> None:
    # FAILED is terminal for a refresh candidate: it is never served and nothing
    # moves it back, so its physical schema can be dropped without a claim.
    schema = await TenantSchema.objects.filter(
        id=schema_id, state=SchemaState.FAILED, load_workspace_id__isnull=True
    ).afirst()
    if schema is None:
        return
    await asyncio.to_thread(SchemaManager().teardown, schema)


def _reconcile_tenant_refreshes(tenant_id) -> LegacyRefreshReconciliation:
    tenant = Tenant.objects.get(id=tenant_id)
    legacy_jobs = find_legacy_refresh_jobs(tenant)
    with transaction.atomic():
        return settle_finished_refresh_candidates(tenant, legacy_jobs)


async def reconcile_refresh_candidates() -> dict:
    """Settle refresh candidates whose queue job finished or whose worker died.

    A refresh worker killed mid-run leaves its job "doing" and its candidate
    PROVISIONING, which blocks every later refresh and keeps the status endpoint
    reporting "provisioning". The refresh endpoint also reconciles on each POST;
    this sweep makes the dead refresh visible (FAILED, logged at error) without
    anyone having to retry first.
    """
    tenant_ids = [
        tenant_id
        async for tenant_id in TenantSchema.objects.filter(state=SchemaState.PROVISIONING)
        .values_list("tenant_id", flat=True)
        .distinct()
    ]
    settled = 0
    recovery_needed = 0
    for tenant_id in tenant_ids:
        try:
            result = await _to_thread_fresh_db(_reconcile_tenant_refreshes, tenant_id)
        except Exception:
            logger.exception("reconcile_refresh_candidates: tenant %s failed", tenant_id)
            continue
        settled += len(result.settled_schema_ids)
        recovery_needed += result.recovery_needed
    return {"settled": settled, "recovery_needed": recovery_needed}


async def expire_inactive_schemas() -> None:
    """Mark stale schemas for teardown and dispatch teardown tasks.

    Handles both TenantSchema and WorkspaceViewSchema records.
    Schemas with null last_accessed_at are never auto-expired.

    The data-bearing MaterializationRun rows are NOT touched here. A schema in
    TEARDOWN is already unreachable via the catalog (load_tenant_context only
    resolves ACTIVE schemas), so flipping runs to STALE before the
    physical DROP succeeds buys nothing — and if teardown_schema later fails the
    DROP and reverts the schema to ACTIVE, prematurely-staled runs would strand
    the (still-present) data as invisible. teardown_schema marks the runs STALE
    only after manager.teardown actually drops the schema.
    """
    cutoff = timezone.now() - timedelta(hours=settings.SCHEMA_TTL_HOURS)

    # Log last_accessed_at BEFORE flipping — teardown/provision later overwrites
    # it, and that timestamp was the forensic input the 2026-06-10 incident review
    # could not recover (arch #257, finding 08#9).
    async for schema in TenantSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        logger.info(
            "expire_inactive_schemas: marking tenant schema %s (%s) for teardown — "
            "last_accessed_at=%s cutoff=%s ttl_hours=%s",
            schema.id,
            schema.schema_name,
            schema.last_accessed_at.isoformat() if schema.last_accessed_at else None,
            cutoff.isoformat(),
            settings.SCHEMA_TTL_HOURS,
        )
        schema.state = SchemaState.TEARDOWN
        await schema.asave(update_fields=["state"])
        await adefer_teardown_schema(schema_id=str(schema.id))

    # Expire stale view schemas (same forensic logging as tenant schemas above).
    async for vs in WorkspaceViewSchema.objects.filter(
        state=SchemaState.ACTIVE,
        last_accessed_at__lt=cutoff,
    ):
        logger.info(
            "expire_inactive_schemas: marking view schema %s (%s) for teardown — "
            "last_accessed_at=%s cutoff=%s ttl_hours=%s",
            vs.id,
            vs.schema_name,
            vs.last_accessed_at.isoformat() if vs.last_accessed_at else None,
            cutoff.isoformat(),
            settings.SCHEMA_TTL_HOURS,
        )
        vs.state = SchemaState.TEARDOWN
        await vs.asave(update_fields=["state"])
        await adefer_teardown_view_schema(view_schema_id=str(vs.id))


async def teardown_view_schema(view_schema_id: str) -> None:
    """Drop the physical PostgreSQL schema for a WorkspaceViewSchema and mark EXPIRED."""
    try:
        vs = await WorkspaceViewSchema.objects.aget(id=view_schema_id)
    except WorkspaceViewSchema.DoesNotExist:
        logger.exception("teardown_view_schema_task: view schema %s not found", view_schema_id)
        return

    # State CAS (arch #237, finding 03#0): abort if no longer TEARDOWN — a
    # rebuild → ACTIVE after queueing means the physical schema is live again.
    if vs.state != SchemaState.TEARDOWN:
        logger.info(
            "teardown_view_schema_task: view schema %s is %s (not TEARDOWN) — "
            "aborting drop; the row was likely reactivated after teardown was queued",
            vs.id,
            vs.state,
        )
        return

    manager = SchemaManager()
    # Both outcome writes are conditional: adding or removing a source can mark
    # the row PROVISIONING while the DROP runs, and its queued rebuild would skip
    # a row this task then blindly marked EXPIRED.
    retiring = WorkspaceViewSchema.objects.filter(pk=vs.pk, state=SchemaState.TEARDOWN)
    try:
        await asyncio.to_thread(manager.teardown_view_schema, vs)
    except Exception:
        logger.exception("Failed to drop view schema '%s'", vs.schema_name)
        await retiring.aupdate(state=SchemaState.ACTIVE)
        raise

    # Destructive op must leave a trace (arch #257, finding 08#9).
    logger.info(
        "teardown_view_schema_task: DROP SCHEMA CASCADE succeeded for view schema %s (%s) — "
        "last_accessed_at=%s",
        vs.id,
        vs.schema_name,
        vs.last_accessed_at.isoformat() if vs.last_accessed_at else None,
    )

    await retiring.aupdate(state=SchemaState.EXPIRED)


# Retirement retry backoff: a sibling workspace still reading the old schema
# rebuilds on its own W; retry until its views have moved, never drop under them.
_RETIRE_RETRY_BASE_SECONDS = 300


_RETIRE_RETRY_MAX_SECONDS = 3600


# About a day at the capped interval; past that a human has to look.
_RETIRE_MAX_ATTEMPTS = 30


async def teardown_schema(schema_id: str, attempt: int = 0) -> None:
    """Retire a tenant schema once nothing outside it depends on it, then mark EXPIRED.

    Holds T for the tenant so no writer can promote or plan against the schema
    while it is inspected. The physical drop is RESTRICT-only and refuses while a
    sibling workspace's views still read the schema; that sibling is asked to
    rebuild and retirement is retried with backoff. Sibling work is only
    deferred, never awaited under T.

    Retirement is best-effort, not a guarantee that expired data stops being
    readable: if a dependent never moves (its rebuild keeps failing, or it is
    not a workspace view schema at all), the row stays TEARDOWN, the schema
    stays physically present, and the dependent keeps reading it. That was
    chosen over cascading under a live reader. The give-up is logged at ERROR
    ("giving up retiring schema"), which reaches Sentry, so an operator can
    move or drop the dependent and re-run the teardown. (A later provision()
    of a canonical-named schema may also reclaim the row as ACTIVE; an ``_r_``
    refresh schema is never reclaimed.)
    """
    try:
        schema = await TenantSchema.objects.aget(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.exception("teardown_schema: schema %s not found", schema_id)
        return

    # State CAS (arch #237, finding 03#0): provision() resurrects EXPIRED rows, and
    # reclaims a canonical-named TEARDOWN row, as ACTIVE (2026-06-10 incident-b fix). If that raced ahead of this queued
    # teardown the re-provisioned data must be preserved — abort unless still TEARDOWN.
    if schema.state != SchemaState.TEARDOWN:
        logger.info(
            "teardown_schema: schema %s is %s (not TEARDOWN) — aborting drop; "
            "the row was likely resurrected by provision() after teardown was queued",
            schema.id,
            schema.state,
        )
        return

    try:
        retired = await _retire_under_tenant_lock(schema, attempt)
    except _RetirementNotStarted as exc:
        # Nothing was dropped. The TTL sweep never re-arms a TEARDOWN row, and
        # provision() reclaims only a canonical-named one, so retry here.
        # Usually plain contention (another load holds T), so no traceback.
        logger.warning(
            "teardown_schema: could not start retiring schema %s: %r", schema.id, exc.__cause__
        )
        await _retry_retirement(schema, [], attempt, str(exc.__cause__ or exc))
        return
    except Exception:
        if schema.state == SchemaState.ACTIVE:
            # The failed drop reverted the row to ACTIVE, but siblings that rebuilt
            # while it was TEARDOWN left this tenant out of their views. Enqueued
            # here because T has been released by now.
            await _rebuild_reverted_dependents(schema)
        raise
    if retired:
        # Dependent view schemas that still list this tenant in their coverage
        # are reconciled after T is released: rebuilt against a surviving
        # ACTIVE schema, or failed truthfully when pure TTL expiry left no data.
        await _reconcile_dependent_view_schemas_after_teardown(schema)


async def _rebuild_reverted_dependents(schema) -> None:
    """Put a reverted schema's tenant back into its dependents' views."""
    try:
        await publication.rebuild_dependent_view_schemas([schema.tenant_id])
    except Exception:
        # Must not mask the retirement failure the caller is about to re-raise.
        logger.exception(
            "teardown_schema: failed to queue dependent rebuilds after reverting schema %s",
            schema.id,
        )


class _RetirementNotStarted(Exception):
    """Taking T or re-reading the row failed before retirement touched anything."""


async def _retire_under_tenant_lock(schema, attempt: int) -> bool:
    """Retire ``schema`` while holding T; True once it is physically dropped."""
    manager = SchemaManager()
    async with contextlib.AsyncExitStack() as stack:
        try:
            acquired = await stack.enter_async_context(tenant_data_lock_if_free(schema.tenant_id))
            if not acquired:
                await _retry_retirement(
                    schema,
                    [],
                    attempt,
                    "tenant lock T is held by an active writer",
                )
                return False
            await schema.arefresh_from_db()
        except TenantSchema.DoesNotExist:
            return False  # deleted (e.g. with its tenant) while we waited for T
        except (
            DataLockTimeout,
            # Unreachable or closed sessions, from the raw lock session or the
            # ORM (Django wraps psycopg's InterfaceError outside DatabaseError).
            psycopg.OperationalError,
            psycopg.InterfaceError,
            DjangoOperationalError,
            DjangoInterfaceError,
        ) as exc:
            # A query bug (ProgrammingError and friends) is deliberately absent:
            # it must surface, not be retried as contention.
            raise _RetirementNotStarted from exc
        if schema.state != SchemaState.TEARDOWN:
            return False
        try:
            await run_data_thread(manager.retire_tenant_schema, schema)
        except SchemaStillReferenced as exc:
            await _retry_retirement(
                schema,
                exc.dependents,
                attempt,
                str(exc),
                converges=exc.converges,
            )
            return False
        except (
            psycopg.errors.LockNotAvailable,
            psycopg.errors.QueryCanceled,
            psycopg.errors.DeadlockDetected,
        ) as exc:
            # A reader held the relations past the retirement lock timeout (or a
            # statement_timeout at or below it, 57014), or locked them in the
            # opposite order; the transaction rolled back and nothing was dropped,
            # so simply try again later.
            await _retry_retirement(schema, [], attempt, str(exc))
            return False
        except Exception as exc:
            superseded = (
                await TenantSchema.objects.filter(
                    tenant_id=schema.tenant_id, state=SchemaState.ACTIVE
                )
                .exclude(id=schema.id)
                .aexists()
            )
            if superseded:
                # A newer ACTIVE schema serves this tenant: reverting would create
                # a second ACTIVE row. provision() can reclaim only a canonical-named
                # schema, so a stranded _r_ refresh schema needs this retry.
                logger.exception(
                    "teardown_schema: retiring superseded schema %s failed; retained", schema.id
                )
                await _retry_retirement(schema, [], attempt, str(exc))
                return False
            # Nothing else serves this tenant and the physical schema still exists —
            # revert to ACTIVE rather than stranding readable data in TEARDOWN.
            # Touching last_accessed_at gives it a full TTL before the sweep retries;
            # otherwise a drop that keeps failing would flip sibling views off and
            # back on every sweep. The cost: a transient failure now waits a TTL too.
            logger.warning(
                "teardown_schema: reverting schema %s to ACTIVE after a failed drop — "
                "last_accessed_at=%s reset to now",
                schema.id,
                schema.last_accessed_at.isoformat() if schema.last_accessed_at else None,
            )
            schema.state = SchemaState.ACTIVE
            schema.last_accessed_at = timezone.now()
            await schema.asave(update_fields=["state", "last_accessed_at"])
            raise

        # Destructive op must leave a forensic trace (arch #257, finding 08#9).
        logger.info(
            "teardown_schema: DROP SCHEMA (RESTRICT) succeeded for retired tenant schema %s (%s) — "
            "last_accessed_at=%s",
            schema.id,
            schema.schema_name,
            schema.last_accessed_at.isoformat() if schema.last_accessed_at else None,
        )

        # Tables are now dropped: flip data-bearing runs to STALE so
        # pipeline_list_tables stops returning ghosts. Done after the DROP succeeds
        # (not at TEARDOWN-flip) so a failed DROP never strands intact data as invisible.
        await MaterializationRun.objects.filter(
            tenant_schema=schema,
            state__in=[
                MaterializationRun.RunState.COMPLETED,
                MaterializationRun.RunState.PARTIAL,
            ],
        ).aupdate(state=MaterializationRun.RunState.STALE)

        try:
            schema.state = SchemaState.EXPIRED
            await schema.asave(update_fields=["state"])
        except Exception:
            # Physical schema is already dropped; don't pretend it's ACTIVE.
            logger.exception(
                "teardown_schema: failed to mark schema %s EXPIRED after teardown", schema.id
            )
            raise
    return True


async def _retry_retirement(
    schema, dependents: list[dict], attempt: int, reason: str, *, converges: bool = True
) -> None:
    """Keep a still-referenced schema, move its readers, and try again later."""
    if not converges or attempt + 1 >= _RETIRE_MAX_ATTEMPTS:
        logger.error(
            "teardown_schema: giving up retiring schema %s (%s) after attempt %d: %s — "
            "left in TEARDOWN; find what still depends on it",
            schema.id,
            schema.schema_name,
            attempt + 1,
            reason,
        )
        return
    dependent_schemas = sorted({d["schema"] for d in dependents if d.get("schema")})
    logger.warning(
        "teardown_schema: retiring schema %s (%s) deferred after attempt %d (%d dependent "
        "schemas): %s",
        schema.id,
        schema.schema_name,
        attempt + 1,
        len(dependent_schemas),
        reason,
    )
    known = 0
    async for vs in WorkspaceViewSchema.objects.filter(schema_name__in=dependent_schemas):
        known += 1
        if vs.state == SchemaState.EXPIRED:
            # Its views were dropped after our attempt looked, so a plain retry
            # converges; a rebuild would only resurrect it as ACTIVE.
            continue
        try:
            if vs.state == SchemaState.TEARDOWN:
                await adefer_teardown_view_schema(view_schema_id=str(vs.id))
            else:
                await adefer_rebuild_workspace_view_schema(workspace_id=str(vs.workspace_id))
        except Exception:
            logger.exception("Failed to defer dependent rebuild for view schema %s", vs.id)
    if dependent_schemas and not known:
        # Only something we cannot rebuild (e.g. a dbt view in another tenant
        # schema) reads this schema; retrying for a day would move nothing.
        await _retry_retirement(
            schema,
            [],
            attempt,
            f"{reason} (no dependent is a workspace view schema)",
            converges=False,
        )
        return
    delay = min(_RETIRE_RETRY_BASE_SECONDS * (2**attempt), _RETIRE_RETRY_MAX_SECONDS)
    await configure_teardown_schema(schedule_in={"seconds": delay}).defer_async(
        schema_id=str(schema.id), attempt=attempt + 1
    )


async def _reconcile_dependent_view_schemas_after_teardown(schema) -> None:
    """Reconcile dependent multi-tenant view schemas after ``schema`` is dropped.

    If the tenant has another ACTIVE schema (refresh path), rebuild the views
    against it; if none survives (pure TTL expiry), flip them FAILED so the catalog
    reports the truth instead of serving an empty view.

    ``exclude(id=schema.id)`` matters: a direct caller may pass an ACTIVE row, so
    excluding it makes the "another ACTIVE schema?" check correct either way.
    """
    tenant_has_surviving_active_schema = (
        await TenantSchema.objects.filter(
            tenant_id=schema.tenant_id,
            state=SchemaState.ACTIVE,
        )
        .exclude(id=schema.id)
        .aexists()
    )
    if tenant_has_surviving_active_schema:
        await publication.rebuild_dependent_view_schemas([schema.tenant_id])
    else:
        # Log the count so a cascade that silently degrades N workspaces is
        # visible (arch #257, finding 08#9).
        failed_count = await _fail_dependent_view_schemas(schema.tenant_id)
        if failed_count:
            logger.warning(
                "teardown_schema: %d dependent multi-tenant view schema(s) flipped to "
                "FAILED after tenant schema %s (%s) was dropped (their namespaced views "
                "were cascade-dropped and the tenant has no surviving data)",
                failed_count,
                schema.id,
                schema.schema_name,
            )


async def _fail_dependent_view_schemas(tenant_id) -> int:
    """Flip every ACTIVE WorkspaceViewSchema depending on ``tenant_id`` to FAILED.

    Only ACTIVE rows — TEARDOWN/FAILED/EXPIRED must not be clobbered out of their
    lifecycle state. Returns the number of rows flipped.
    """
    dependent_workspace_ids = (
        Workspace.objects.filter(workspace_tenants__tenant_id=tenant_id)
        .annotate(num_tenants=publication.multi_tenant_count_subquery())
        .filter(num_tenants__gte=2)
        .values("id")
    )
    # Truthful last_error for the cascade (07#9): the marker lets the resume logic
    # advise a re-run, instead of the generic "system-side fix required" — wrong
    # here, since re-materializing the torn-down tenant IS the fix.
    # Older builds have no coverage record; conservatively retain their legacy
    # invalidation. Explicitly excluded sources have no views to cascade-drop.
    excluded = Q(tenant_coverage__contains={"excluded_tenants": [{"tenant_id": str(tenant_id)}]})
    included = Q(tenant_coverage__contains={"included_tenants": [{"tenant_id": str(tenant_id)}]})
    return (
        await WorkspaceViewSchema.objects.filter(
            workspace_id__in=dependent_workspace_ids,
            state=SchemaState.ACTIVE,
        )
        .exclude(excluded & ~included)
        .aupdate(
            state=SchemaState.FAILED,
            last_error=VIEW_SCHEMA_CASCADE_TEARDOWN_ERROR,
        )
    )
