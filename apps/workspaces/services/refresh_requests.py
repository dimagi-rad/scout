"""Durable ownership and single-execution claims for tenant refreshes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.access import workspace_write_allowed
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant

REFRESH_TASK_NAME = "apps.workspaces.tasks.refresh_tenant_schema"
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled", "aborted"})
_LEGACY_ARG_KEYS = frozenset({"schema_id", "membership_id"})
_CONTEXT_ARG_KEYS = frozenset({"schema_id", "membership_id", "actor_user_id", "workspace_id"})


@dataclass(frozen=True)
class RefreshClaim:
    status: str
    schema: TenantSchema | None = None
    membership: TenantMembership | None = None


@dataclass(frozen=True)
class LegacyRefreshReconciliation:
    reconciled: int = 0
    recovery_needed: bool = False


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


def _settle_denied_candidate(schema: TenantSchema) -> RefreshClaim:
    schema.state = SchemaState.FAILED
    schema.save(update_fields=["state"])
    return RefreshClaim(status="denied")


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
        return RefreshClaim(status="denied")
    tenant_id = (
        TenantSchema.objects.filter(id=parsed_schema_id).values_list("tenant_id", flat=True).first()
    )
    if tenant_id is None:
        return RefreshClaim(status="denied")

    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant_id)
        try:
            schema = (
                TenantSchema.objects.select_for_update()
                .select_related("tenant")
                .get(id=parsed_schema_id, tenant_id=tenant_id)
            )
            job = ProcrastinateJob.objects.select_for_update().get(id=job_id)
        except (TenantSchema.DoesNotExist, ProcrastinateJob.DoesNotExist):
            return RefreshClaim(status="denied")

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
            return RefreshClaim(status="denied")

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
        except (TenantMembership.DoesNotExist, TypeError, ValueError, ValidationError):
            return _settle_denied_candidate(schema)

        workspace_matches = WorkspaceTenant.objects.filter(
            workspace_id=schema.refresh_workspace_id,
            tenant_id=schema.tenant_id,
        ).exists()
        if not workspace_matches or not workspace_write_allowed(
            membership.user, schema.refresh_workspace_id
        ):
            return _settle_denied_candidate(schema)

        schema.refresh_claimed_at = timezone.now()
        schema.save(update_fields=["refresh_claimed_at"])
        return RefreshClaim(status="claimed", schema=schema, membership=membership)


def fail_claimed_refresh_candidate(schema_id, job_id: int) -> TenantSchema | None:
    """CAS one claimed candidate to FAILED and return it for physical cleanup."""
    try:
        schema = TenantSchema.objects.select_related("tenant").get(id=schema_id)
    except (TenantSchema.DoesNotExist, TypeError, ValueError, ValidationError):
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


def activate_claimed_refresh_candidate(schema_id, job_id: int, accessed_at) -> bool:
    """Publish only the candidate claimed by this exact queue job."""
    return bool(
        TenantSchema.objects.filter(
            id=schema_id,
            refresh_job_id=job_id,
            state=SchemaState.PROVISIONING,
            refresh_claimed_at__isnull=False,
        ).update(state=SchemaState.ACTIVE, last_accessed_at=accessed_at)
    )


def reconcile_legacy_refresh_candidates(tenant) -> LegacyRefreshReconciliation:
    """Settle only provably terminal legacy refresh candidates for a retry.

    The caller may already hold the tenant lock; taking it again is harmless and
    keeps the required Tenant -> candidate -> queue-job order for direct callers.
    Active jobs remain ordinary in-progress work. Ambiguous, malformed, or
    cross-tenant evidence remains untouched and is flagged for operator recovery.
    """
    reconciled = 0
    recovery_needed = False
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant.id)
        candidates = list(
            TenantSchema.objects.select_for_update().filter(
                tenant=tenant,
                state=SchemaState.PROVISIONING,
            )
        )
        for candidate in candidates:
            if candidate.refresh_job_id is not None:
                try:
                    job = ProcrastinateJob.objects.select_for_update().get(
                        id=candidate.refresh_job_id
                    )
                except ProcrastinateJob.DoesNotExist:
                    recovery_needed = True
                    continue
                expected_args = refresh_task_args(candidate)
                if (
                    expected_args is None
                    or job.task_name != REFRESH_TASK_NAME
                    or job.args != expected_args
                ):
                    recovery_needed = True
                    continue
                if job.status in {"todo", "doing", "aborting"}:
                    continue
                if job.status in _TERMINAL_JOB_STATUSES:
                    candidate.state = SchemaState.FAILED
                    candidate.save(update_fields=["state"])
                    reconciled += 1
                    continue
                recovery_needed = True
                continue
            jobs = list(
                ProcrastinateJob.objects.select_for_update().filter(
                    task_name=REFRESH_TASK_NAME,
                    args__schema_id=str(candidate.id),
                )[:2]
            )
            if len(jobs) != 1:
                recovery_needed = True
                continue
            job = jobs[0]
            args = job.args if isinstance(job.args, dict) else {}
            keys = frozenset(args)
            if keys not in {_LEGACY_ARG_KEYS, _CONTEXT_ARG_KEYS}:
                recovery_needed = True
                continue
            if _parsed_uuid(args.get("schema_id")) != candidate.id:
                recovery_needed = True
                continue
            membership_id = _parsed_uuid(args.get("membership_id"))
            if membership_id is None:
                recovery_needed = True
                continue
            membership = TenantMembership.all_objects.filter(
                id=membership_id, tenant_id=candidate.tenant_id
            ).first()
            if membership is None:
                recovery_needed = True
                continue
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
                    recovery_needed = True
                    continue
            if job.status in {"todo", "doing", "aborting"}:
                continue
            if job.status in _TERMINAL_JOB_STATUSES:
                candidate.state = SchemaState.FAILED
                candidate.save(update_fields=["state"])
                reconciled += 1
                continue
            recovery_needed = True
    return LegacyRefreshReconciliation(
        reconciled=reconciled,
        recovery_needed=recovery_needed,
    )
