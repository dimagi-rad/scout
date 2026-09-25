"""Candidate schemas for shared-tenant loads: open, promote, fail and settle.

A load never writes the schema other workspaces are reading. It fills a
PROVISIONING candidate while holding the tenant lock T, and a single control
transaction then makes the candidate ACTIVE and demotes the old ACTIVE schema,
so there is never a two-ACTIVE interval. A failed candidate keeps its physical
data: the next load of the same pending generation with the same raw-load
configuration resumes it (the materializer continues resumable sources from
its last committed cursor). Every other failed candidate is abandoned and
dropped by a bounded-retry cleanup.

Lock order is T (the tenant advisory lock), then the Tenant row, then its
TenantSchema rows. T's holder waits on an idle side session, so PostgreSQL
cannot see a cycle: never wait on T while holding the Tenant row.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.common.identifiers import refresh_schema_name
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
)
from apps.workspaces.services.data_operation import assert_tenant_lock_held
from apps.workspaces.services.load_generations import (
    publish_generation,
    resumable_candidate,
    transforms_publishable,
)


def load_owner_token(job_id: int | None) -> int:
    """The candidate owner id for one load attempt.

    A load outside a queue job (the agent's blocking tool) gets a fresh negative
    token: procrastinate ids are positive, so it never matches a real job, and it
    differs per attempt, so one job-less attempt cannot pass for another.
    """
    if job_id is not None:
        return job_id
    return -(secrets.randbits(62) + 1)


@dataclass(frozen=True)
class OpenedCandidate:
    schema: TenantSchema
    resumed: bool


@dataclass(frozen=True)
class Promotion:
    promoted: bool
    retired_schema_ids: tuple[uuid.UUID, ...] = ()


def open_workspace_candidate(
    tenant, *, workspace_id, job_id, generation: int, config_fingerprint: str
) -> OpenedCandidate:
    """Resume this generation's matching FAILED candidate, or create a fresh one.

    The caller must hold T for ``tenant``, so no other writer can be filling or
    resuming a candidate concurrently; the Tenant row lock orders this against
    promotion and the reconciler.
    """
    if job_id is None:
        # Promotion refuses a None owner, so such a candidate could never publish.
        raise ValueError("job_id is required; use load_owner_token() for a job-less load")
    assert_tenant_lock_held(tenant.id)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant.id)
        match = resumable_candidate(tenant.id, generation, config_fingerprint)
        if match is not None:
            resumed = TenantSchema.objects.filter(
                id=match.id,
                state=SchemaState.FAILED,
                load_generation=generation,
                load_config_fingerprint=config_fingerprint,
            ).update(
                state=SchemaState.PROVISIONING,
                load_workspace_id=workspace_id,
                load_job_id=job_id,
            )
            if resumed:
                match.refresh_from_db()
                return OpenedCandidate(schema=match, resumed=True)
        schema = TenantSchema.objects.create(
            tenant=tenant,
            schema_name=refresh_schema_name(
                tenant.provider, tenant.external_id, token=uuid.uuid4().hex[:8]
            ),
            state=SchemaState.PROVISIONING,
            load_workspace_id=workspace_id,
            load_job_id=job_id,
            load_generation=generation,
            load_config_fingerprint=config_fingerprint,
        )
        return OpenedCandidate(schema=schema, resumed=False)


def _run_is_publishable(run: MaterializationRun | None, fingerprint: str) -> bool:
    result = run.result if run is not None else None
    if not isinstance(result, dict) or not isinstance(result.get("sources"), dict):
        return False
    if not fingerprint or result.get("load_fingerprint") != fingerprint:
        return False
    return not (result.get("cancelled") or result.get("error")) and transforms_publishable(result)


def promote_candidate_schema(
    candidate_id,
    *,
    accessed_at,
    loading_generation: int,
    run_id,
    fingerprint: str,
    refresh_job_id: int | None = None,
    workspace_id=None,
    workspace_job_id: int | None = None,
) -> Promotion:
    """Publish one candidate as the tenant's ACTIVE schema in a single transaction.

    Lock order is Tenant, then the tenant's TenantSchema rows. The candidate must
    still be PROVISIONING and owned by the caller (the exact claimed refresh job,
    or the workspace load that opened it), and must carry an owned, COMPLETED,
    fingerprinted run of the generation being loaded. Every other ACTIVE schema
    becomes TEARDOWN in the same transaction and the generation is published
    under the same commit. Earlier runs on a resumed candidate are superseded
    (STALE) here, after the load consumed their resume cursors, so the published
    run is the only live evidence and the generation stays reusable.

    Nothing sweeps stranded TEARDOWN rows: the caller must queue a teardown for
    each of ``retired_schema_ids`` in the same transaction or on commit.

    ``workspace_job_id`` is the candidate owner (``load_owner_token``): a real
    queue job id, which the run must also record, or a negative token for a
    job-less load, whose run records no job.
    """
    tenant_id = (
        TenantSchema.objects.filter(id=candidate_id).values_list("tenant_id", flat=True).first()
    )
    if tenant_id is None:
        return Promotion(promoted=False)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant_id)
        rows = list(
            TenantSchema.objects.select_for_update().filter(
                tenant_id=tenant_id,
                state__in=[SchemaState.PROVISIONING, SchemaState.ACTIVE],
            )
        )
        candidate = next((row for row in rows if row.id == candidate_id), None)
        if candidate is None or candidate.state != SchemaState.PROVISIONING:
            return Promotion(promoted=False)
        if refresh_job_id is not None:
            owned = (
                candidate.refresh_job_id == refresh_job_id
                and candidate.refresh_claimed_at is not None
            )
            job_id = refresh_job_id
        else:
            # Retiring runs the caller does not own below is only safe under T.
            assert_tenant_lock_held(tenant_id)
            # None would match any job-less candidate and run; job-less loads use
            # load_owner_token instead, whose run records no queue job.
            owned = (
                candidate.load_workspace_id is not None
                and candidate.load_workspace_id == workspace_id
                and workspace_job_id is not None
                and candidate.load_job_id == workspace_job_id
            )
            job_id = workspace_job_id if workspace_job_id and workspace_job_id > 0 else None
        if not owned:
            return Promotion(promoted=False)

        run = (
            MaterializationRun.objects.select_for_update()
            .filter(
                id=run_id,
                tenant_schema_id=candidate.id,
                procrastinate_job_id=job_id,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .first()
            if run_id
            else None
        )
        if not _run_is_publishable(run, fingerprint):
            return Promotion(promoted=False)
        generation = (
            TenantLoadGeneration.objects.select_for_update().filter(tenant_id=tenant_id).first()
        )
        if (
            generation is None
            # Only the generation this writer began may publish; a request that
            # arrived mid-load has already moved requested_generation past it.
            or loading_generation != generation.loading_generation
            or loading_generation <= generation.published_generation
        ):
            return Promotion(promoted=False)

        # Every other run on the candidate is an earlier attempt's, including a
        # dead writer's run stuck in an active state: a workspace candidate is
        # written only under T (asserted above), and a refresh candidate only by
        # the one job its unique refresh_job_id binds it to.
        MaterializationRun.objects.filter(tenant_schema_id=candidate.id).exclude(id=run.id).exclude(
            state=MaterializationRun.RunState.STALE
        ).update(state=MaterializationRun.RunState.STALE)
        retired = tuple(row.id for row in rows if row.state == SchemaState.ACTIVE)
        TenantSchema.objects.filter(id__in=retired).update(state=SchemaState.TEARDOWN)
        candidate.state = SchemaState.ACTIVE
        candidate.last_accessed_at = accessed_at
        # This marker identifies an unpublished candidate. Clear it on publish
        # so a later EXPIRED state cannot make a once-served run look unpublished.
        candidate.load_workspace_id = None
        candidate.save(update_fields=["state", "last_accessed_at", "load_workspace_id"])
        publish_generation(tenant_id, loading_generation, run, candidate, fingerprint)
        return Promotion(promoted=True, retired_schema_ids=retired)


def fail_workspace_candidate(candidate_id, workspace_id, job_id) -> TenantSchema | None:
    """CAS this load's candidate to FAILED, keeping its data for a resume."""
    try:
        schema = TenantSchema.objects.select_related("tenant").get(id=candidate_id)
    except TenantSchema.DoesNotExist:
        return None
    changed = TenantSchema.objects.filter(
        id=schema.id,
        load_workspace_id=workspace_id,
        load_job_id=job_id,
        state=SchemaState.PROVISIONING,
    ).update(state=SchemaState.FAILED)
    if not changed:
        return None
    schema.state = SchemaState.FAILED
    return schema


def settle_orphaned_workspace_candidates(tenant_id) -> list[TenantSchema]:
    """Under T, mark workspace candidates whose writer died as FAILED.

    A writer holds T for its candidate's whole PROVISIONING life, so a caller
    holding T sees only orphans. They become FAILED, which keeps them eligible
    for resume by the same pending generation. Raises LockOrderError without
    T: a caller without it would fail a live writer's load underneath it.
    """
    assert_tenant_lock_held(tenant_id)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant_id)
        orphans = list(
            TenantSchema.objects.select_for_update().filter(
                tenant_id=tenant_id,
                state=SchemaState.PROVISIONING,
                load_workspace_id__isnull=False,
            )
        )
        if orphans:
            TenantSchema.objects.filter(id__in=[o.id for o in orphans]).update(
                state=SchemaState.FAILED
            )
            # The dead writer's run would otherwise report the tenant as loading
            # forever. FAILED (not STALE) keeps its cursors resumable.
            MaterializationRun.objects.filter(
                tenant_schema_id__in=[o.id for o in orphans],
                state__in=list(MaterializationRun.ACTIVE_STATES),
            ).update(state=MaterializationRun.RunState.FAILED, completed_at=timezone.now())
        for orphan in orphans:
            orphan.state = SchemaState.FAILED
        return orphans


def _forget_resume_evidence(candidates) -> None:
    # resumable_candidate requires a matching config fingerprint, so clearing it
    # takes a candidate out of resume for good.
    TenantSchema.objects.filter(id__in=[c.id for c in candidates]).update(
        load_config_fingerprint=""
    )


def abandoned_workspace_candidates(tenant_id, *, keep_id) -> list[TenantSchema]:
    """FAILED workspace candidates no load will resume, other than ``keep_id``.

    Called by the writer under T once it has chosen its candidate (the resumed
    one is ``keep_id``); only that one can match the pending generation's
    config, so every other FAILED candidate is abandoned partial data.
    ``keep_id`` is required: excluding None would exclude nothing and hand the
    resumable candidate to cleanup. The returned rows lose their resume evidence
    here, under T, so none can be resumed once cleanup has dropped its schema.
    """
    if keep_id is None:
        raise ValueError("keep_id is required; None would hand the resumable candidate to cleanup")
    assert_tenant_lock_held(tenant_id)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant_id)
        abandoned = list(
            TenantSchema.objects.filter(
                tenant_id=tenant_id,
                state=SchemaState.FAILED,
                load_workspace_id__isnull=False,
            ).exclude(id=keep_id)
        )
        _forget_resume_evidence(abandoned)
        return abandoned
