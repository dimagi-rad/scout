"""Candidate schemas for shared-tenant loads: open, promote, fail and settle.

A load never writes the schema other workspaces are reading. It fills a
PROVISIONING candidate while holding the tenant lock T, and a single control
transaction then makes the candidate ACTIVE and demotes the old ACTIVE schema,
so there is never a two-ACTIVE interval. A failed candidate keeps its physical
data: the next load of the same pending generation with the same raw-load
configuration resumes it (the materializer continues resumable sources from
its last committed cursor). Every other failed candidate is abandoned and
dropped by a bounded-retry cleanup.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.common.identifiers import refresh_schema_name
from apps.transformations.models import TransformationRunStatus
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
)
from apps.workspaces.services.load_generations import publish_generation, resumable_candidate

_SUPERSEDABLE_RUN_STATES = (
    MaterializationRun.RunState.COMPLETED,
    MaterializationRun.RunState.PARTIAL,
    MaterializationRun.RunState.FAILED,
    MaterializationRun.RunState.CANCELLED,
)


@dataclass(frozen=True)
class OpenedCandidate:
    schema: TenantSchema
    resumed: bool


@dataclass(frozen=True)
class Promotion:
    promoted: bool
    retired_schema_ids: tuple = ()


def open_workspace_candidate(
    tenant, *, workspace_id, job_id, generation: int, config_fingerprint: str
) -> OpenedCandidate:
    """Resume this generation's matching FAILED candidate, or create a fresh one.

    The caller holds T for ``tenant``, so no other writer can be filling or
    resuming a candidate concurrently; the Tenant row lock orders this against
    promotion and the reconciler.
    """
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
    transforms = result.get("transforms") or {}
    return not (
        result.get("cancelled")
        or result.get("error")
        or result.get("transform_error")
        or not isinstance(transforms, dict)
        or (
            transforms.get("error")
            and transforms.get("status") != TransformationRunStatus.TESTS_FAILED
        )
    )


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
            # A None job would match any job-less run on the schema, so a load
            # without a job id can never prove it owns the candidate.
            owned = (
                candidate.load_workspace_id is not None
                and candidate.load_workspace_id == workspace_id
                and workspace_job_id is not None
                and candidate.load_job_id == workspace_job_id
            )
            job_id = workspace_job_id
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

        MaterializationRun.objects.filter(
            tenant_schema_id=candidate.id,
            state__in=_SUPERSEDABLE_RUN_STATES,
        ).exclude(id=run.id).update(state=MaterializationRun.RunState.STALE)
        retired = tuple(row.id for row in rows if row.state == SchemaState.ACTIVE)
        TenantSchema.objects.filter(id__in=retired).update(state=SchemaState.TEARDOWN)
        candidate.state = SchemaState.ACTIVE
        candidate.last_accessed_at = accessed_at
        candidate.save(update_fields=["state", "last_accessed_at"])
        publish_generation(tenant_id, loading_generation, run, candidate, fingerprint)
        return Promotion(promoted=True, retired_schema_ids=retired)


def fail_workspace_candidate(candidate_id, workspace_id, job_id) -> TenantSchema | None:
    """CAS this load's candidate to FAILED, keeping its data for a resume."""
    try:
        schema = TenantSchema.objects.select_related("tenant").get(id=candidate_id)
    except (TenantSchema.DoesNotExist, TypeError, ValueError, ValidationError):
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
    for resume by the same pending generation.
    """
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
        for orphan in orphans:
            orphan.state = SchemaState.FAILED
        return orphans


def abandoned_workspace_candidates(tenant_id, *, keep_id) -> list[TenantSchema]:
    """FAILED workspace candidates no load will resume, other than ``keep_id``.

    Called by the writer under T once it has chosen its candidate (the resumed
    one is ``keep_id``); only that one can match the pending generation's
    config, so every other FAILED candidate is abandoned partial data.
    ``keep_id`` is required: excluding None would exclude nothing and hand the
    resumable candidate to cleanup.
    """
    with transaction.atomic():
        Tenant.objects.select_for_update().get(id=tenant_id)
        return list(
            TenantSchema.objects.filter(
                tenant_id=tenant_id,
                state=SchemaState.FAILED,
                load_workspace_id__isnull=False,
            ).exclude(id=keep_id)
        )
