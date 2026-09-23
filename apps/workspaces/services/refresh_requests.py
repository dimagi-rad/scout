"""Durable ownership and single-execution claims for tenant refreshes."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.common.identifiers import tenant_schema_name
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.access import workspace_write_allowed
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant

logger = logging.getLogger(__name__)

REFRESH_TASK_NAME = "apps.workspaces.tasks.refresh_tenant_schema"
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "aborted"})
_LEGACY_ARG_KEYS = frozenset({"schema_id", "membership_id"})
_CONTEXT_ARG_KEYS = frozenset({"schema_id", "membership_id", "actor_user_id", "workspace_id"})


# "rejected" means the queued job is not the request its candidate records, so the
# job must not act at all; "denied" means the exact request was proven but its actor
# no longer has authority, and ``reason`` says which authority was lost.
DENIED_ROLE_REQUIRED = "role_required"
DENIED_MEMBERSHIP_MISSING = "membership_missing"
DENIED_WORKSPACE_UNLINKED = "workspace_unlinked"


@dataclass(frozen=True)
class RefreshClaim:
    status: str
    schema: TenantSchema | None = None
    membership: TenantMembership | None = None
    reason: str = ""


@dataclass(frozen=True)
class LegacyRefreshReconciliation:
    recovery_needed: bool = False
    settled_schema_ids: tuple[uuid.UUID, ...] = ()


def refresh_task_args(schema: TenantSchema) -> dict[str, str] | None:
    """Return the exact queue arguments bound to ``schema``, if fully bound."""
    if (
        schema.refresh_workspace_id is None
        or schema.refresh_actor_user_id is None
        or schema.refresh_membership_id is None
    ):
        return None
    return {
        "schema_id": str(schema.id),
        "membership_id": str(schema.refresh_membership_id),
        "actor_user_id": str(schema.refresh_actor_user_id),
        "workspace_id": str(schema.refresh_workspace_id),
    }


def _parsed_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _parsed_user_id(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if str(parsed) == str(value) else None


def _settle_denied_candidate(schema: TenantSchema, reason: str) -> RefreshClaim:
    schema.state = SchemaState.FAILED
    schema.save(update_fields=["state"])
    return RefreshClaim(status="denied", reason=reason)


def claim_refresh_candidate(
    *,
    schema_id: str,
    membership_id: str,
    actor_user_id: str,
    workspace_id: str,
    job_id: int,
) -> RefreshClaim:
    """Atomically prove and claim one refresh request before physical work.

    Lock order is Tenant -> candidate TenantSchema -> ProcrastinateJob. Binding
    mismatches never mutate the candidate. Once the exact request is proven, a
    lost membership or workspace role may settle its own candidate as FAILED.
    """
    parsed_schema_id = _parsed_uuid(schema_id)
    if parsed_schema_id is None:
        return RefreshClaim(status="rejected")
    tenant_id = (
        TenantSchema.objects.filter(id=parsed_schema_id).values_list("tenant_id", flat=True).first()
    )
    if tenant_id is None:
        return RefreshClaim(status="rejected")

    with transaction.atomic():
        try:
            Tenant.objects.select_for_update().get(id=tenant_id)
            schema = (
                TenantSchema.objects.select_for_update()
                .select_related("tenant")
                .get(id=parsed_schema_id, tenant_id=tenant_id)
            )
            job = ProcrastinateJob.objects.select_for_update().get(id=job_id)
        except (Tenant.DoesNotExist, TenantSchema.DoesNotExist, ProcrastinateJob.DoesNotExist):
            return RefreshClaim(status="rejected")

        expected_args = refresh_task_args(schema)
        supplied_args = {
            "schema_id": schema_id,
            "membership_id": membership_id,
            "actor_user_id": actor_user_id,
            "workspace_id": workspace_id,
        }
        if (
            schema.refresh_job_id != job_id
            or expected_args is None
            or supplied_args != expected_args
            or job.task_name != REFRESH_TASK_NAME
            or job.args != expected_args
            or job.status != "doing"
        ):
            return RefreshClaim(status="rejected")

        if schema.state != SchemaState.PROVISIONING or schema.refresh_claimed_at is not None:
            return RefreshClaim(status="ignored")

        try:
            membership = TenantMembership.objects.select_related(
                "tenant", "user", "connection"
            ).get(
                id=schema.refresh_membership_id,
                user_id=schema.refresh_actor_user_id,
                tenant_id=schema.tenant_id,
            )
        except TenantMembership.DoesNotExist:
            return _settle_denied_candidate(schema, DENIED_MEMBERSHIP_MISSING)

        if not WorkspaceTenant.objects.filter(
            workspace_id=schema.refresh_workspace_id,
            tenant_id=schema.tenant_id,
        ).exists():
            return _settle_denied_candidate(schema, DENIED_WORKSPACE_UNLINKED)
        if not workspace_write_allowed(membership.user, schema.refresh_workspace_id):
            return _settle_denied_candidate(schema, DENIED_ROLE_REQUIRED)

        schema.refresh_claimed_at = timezone.now()
        schema.save(update_fields=["refresh_claimed_at"])
        return RefreshClaim(status="claimed", schema=schema, membership=membership)


def fail_claimed_refresh_candidate(schema_id: uuid.UUID, job_id: int) -> TenantSchema | None:
    """CAS one claimed candidate to FAILED and return it for physical cleanup."""
    try:
        schema = TenantSchema.objects.select_related("tenant").get(id=schema_id)
    except TenantSchema.DoesNotExist:
        logger.warning("Claimed refresh candidate %s vanished before cleanup", schema_id)
        return None
    changed = TenantSchema.objects.filter(
        id=schema.id,
        refresh_job_id=job_id,
        state=SchemaState.PROVISIONING,
        refresh_claimed_at__isnull=False,
    ).update(state=SchemaState.FAILED)
    if not changed:
        return None
    schema.state = SchemaState.FAILED
    return schema


def activate_claimed_refresh_candidate(
    schema_id: uuid.UUID, job_id: int, accessed_at: datetime
) -> bool:
    """Publish only the candidate claimed by this exact queue job."""
    return bool(
        TenantSchema.objects.filter(
            id=schema_id,
            refresh_job_id=job_id,
            state=SchemaState.PROVISIONING,
            refresh_claimed_at__isnull=False,
        ).update(state=SchemaState.ACTIVE, last_accessed_at=accessed_at)
    )


def _job_was_pruned(candidate: TenantSchema, pruned_before: datetime) -> bool:
    """Whether a candidate's missing queue job is explained by retention pruning.

    A candidate is inserted in the same transaction as its job, and a running job's
    row is never pruned, so a missing row older than the retention window can only
    be a finished job that prune_old_procrastinate_jobs removed. A younger gap has
    no such explanation and stays with the operator. ``pruned_before`` is the
    queue's retention cutoff (now minus JOB_RETENTION_HOURS).
    """
    return candidate.created_at < pruned_before


def _unbound_candidates(tenant):
    # provision() holds the tenant's base-named row in PROVISIONING during its
    # CREATE SCHEMA; that row never has a refresh job, so demanding queue
    # evidence for it would misreport an initial load as a broken refresh.
    return TenantSchema.objects.filter(
        tenant=tenant, state=SchemaState.PROVISIONING, refresh_job_id__isnull=True
    ).exclude(schema_name=tenant_schema_name(tenant.provider, tenant.external_id))


def find_legacy_refresh_jobs(tenant) -> dict[uuid.UUID, tuple[int, ...]]:
    """Find the queue jobs of unbound (pre-binding) candidates, before any lock.

    procrastinate_jobs.args has no index for a schema_id lookup, so this scan must
    not run under the tenant lock. Candidates created since binding shipped always
    carry refresh_job_id, so the unbound set and their job ids cannot grow between
    this scan and the locked reconciliation. At most two ids are kept per
    candidate, which is enough to tell "exactly one" from "ambiguous".
    """
    return {
        candidate_id: tuple(
            ProcrastinateJob.objects.filter(
                task_name=REFRESH_TASK_NAME, args__schema_id=str(candidate_id)
            ).values_list("id", flat=True)[:2]
        )
        for candidate_id in _unbound_candidates(tenant).values_list("id", flat=True)
    }


def reconcile_legacy_refresh_candidates(
    tenant,
    legacy_jobs: dict[uuid.UUID, tuple[int, ...]],
    *,
    pruned_before: datetime,
    stalled_before: datetime,
) -> LegacyRefreshReconciliation:
    """Settle only provably finished refresh candidates so a retry can proceed.

    ``legacy_jobs`` comes from ``find_legacy_refresh_jobs`` called before the lock;
    here each job is re-read by primary key under the lock. ``pruned_before`` is
    the queue's retention cutoff and ``stalled_before`` the worker-heartbeat
    cutoff: a job still marked running whose worker has not heartbeated since
    then died with the job and is settled like a finished one. The caller may
    already hold the tenant lock; taking it again is harmless and keeps the
    required Tenant -> candidate -> queue-job order for direct callers.
    Ambiguous, malformed, or cross-tenant evidence remains untouched and is
    flagged for operator recovery.
    """
    settled: list[uuid.UUID] = []
    recovery_needed = False
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant.id)
        unbound = _unbound_candidates(tenant).values_list("id", flat=True)
        candidates = list(
            TenantSchema.objects.select_for_update()
            .filter(tenant=tenant, state=SchemaState.PROVISIONING)
            .filter(Q(refresh_job_id__isnull=False) | Q(id__in=unbound))
        )
        for candidate in candidates:
            if candidate.refresh_job_id is not None:
                outcome, reason = _bound_candidate_outcome(candidate, pruned_before, stalled_before)
            else:
                outcome, reason = _legacy_candidate_outcome(
                    candidate, legacy_jobs.get(candidate.id), pruned_before, stalled_before
                )
            if outcome == _SETTLE:
                candidate.state = SchemaState.FAILED
                candidate.save(update_fields=["state"])
                settled.append(candidate.id)
                log = logger.error if reason == _STALLED else logger.info
                log(
                    "Settled refresh candidate %s (%s) for tenant %s as FAILED: %s",
                    candidate.id,
                    candidate.schema_name,
                    tenant.id,
                    reason,
                )
            elif outcome == _RECOVER:
                recovery_needed = True
                logger.warning(
                    "Refresh candidate %s (%s) for tenant %s needs operator recovery: %s",
                    candidate.id,
                    candidate.schema_name,
                    tenant.id,
                    reason,
                )
    return LegacyRefreshReconciliation(
        recovery_needed=recovery_needed,
        settled_schema_ids=tuple(settled),
    )


_KEEP = "keep"
_SETTLE = "settle"
_RECOVER = "recover"
_STALLED = "queue job's worker stopped heartbeating (worker died mid-refresh)"
_IN_FLIGHT_STATUSES = frozenset({"todo", "doing", "aborting"})


def _job_outcome(job: ProcrastinateJob, stalled_before: datetime) -> tuple[str, str]:
    if job.status in _IN_FLIGHT_STATUSES:
        # Mirrors Procrastinate's own stalled-job rule: a started job whose worker
        # row is gone or whose heartbeat is older than the cutoff will never finish.
        if job.status != "todo" and (
            job.worker is None or job.worker.last_heartbeat < stalled_before
        ):
            return _SETTLE, _STALLED
        return _KEEP, f"queue job {job.id} is {job.status}"
    if job.status in _TERMINAL_JOB_STATUSES:
        return _SETTLE, f"queue job {job.id} ended {job.status}"
    return _RECOVER, f"queue job {job.id} has unknown status {job.status!r}"


def _bound_candidate_outcome(
    candidate: TenantSchema, pruned_before: datetime, stalled_before: datetime
) -> tuple[str, str]:
    try:
        job = (
            ProcrastinateJob.objects.select_for_update(of=("self",))
            .select_related("worker")
            .get(id=candidate.refresh_job_id)
        )
    except ProcrastinateJob.DoesNotExist:
        if _job_was_pruned(candidate, pruned_before):
            return _SETTLE, "queue job was pruned after finishing"
        return _RECOVER, f"queue job {candidate.refresh_job_id} is missing"
    expected_args = refresh_task_args(candidate)
    if expected_args is None or job.task_name != REFRESH_TASK_NAME or job.args != expected_args:
        return _RECOVER, f"queue job {job.id} does not match the recorded request"
    return _job_outcome(job, stalled_before)


def _legacy_candidate_outcome(
    candidate: TenantSchema,
    job_ids: tuple[int, ...] | None,
    pruned_before: datetime,
    stalled_before: datetime,
) -> tuple[str, str]:
    if job_ids is None:
        return _RECOVER, "was not visible to the queue scan"
    jobs = list(
        ProcrastinateJob.objects.select_for_update(of=("self",))
        .select_related("worker")
        .filter(id__in=job_ids, task_name=REFRESH_TASK_NAME)
        .order_by("id")
    )
    if not jobs and _job_was_pruned(candidate, pruned_before):
        return _SETTLE, "queue job was pruned after finishing"
    if len(jobs) != 1:
        return _RECOVER, f"found {len(jobs)} queue jobs, expected exactly one"
    job = jobs[0]
    args = job.args if isinstance(job.args, dict) else {}
    keys = frozenset(args)
    malformed = f"queue job {job.id} has malformed or foreign arguments"
    if keys not in {_LEGACY_ARG_KEYS, _CONTEXT_ARG_KEYS}:
        return _RECOVER, malformed
    if _parsed_uuid(args.get("schema_id")) != candidate.id:
        return _RECOVER, malformed
    membership_id = _parsed_uuid(args.get("membership_id"))
    if membership_id is None:
        return _RECOVER, malformed
    membership = TenantMembership.all_objects.filter(
        id=membership_id, tenant_id=candidate.tenant_id
    ).first()
    if membership is None:
        return _RECOVER, malformed
    if keys == _CONTEXT_ARG_KEYS:
        workspace_id = _parsed_uuid(args.get("workspace_id"))
        actor_user_id = _parsed_user_id(args.get("actor_user_id"))
        if (
            workspace_id is None
            or actor_user_id != membership.user_id
            or not WorkspaceTenant.objects.filter(
                workspace_id=workspace_id, tenant_id=candidate.tenant_id
            ).exists()
        ):
            return _RECOVER, malformed
    return _job_outcome(job, stalled_before)
