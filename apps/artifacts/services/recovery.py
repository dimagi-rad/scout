"""Admit, deduplicate and dispatch the repair of an artifact's data surface.

One active WorkspaceDataRecovery per workspace is enforced by a partial unique
constraint; that constraint, not the state read before the insert, is the
cross-process dedupe guard. The HTTP view only maps the returned admission to a
status code.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from asgiref.sync import sync_to_async
from django.db import IntegrityError

from apps.artifacts.models import Artifact
from apps.artifacts.services.graph_manifest import backfill_missing_semantic_query_manifest
from apps.users.models import User
from apps.workspaces.models import WorkspaceDataRecovery
from apps.workspaces.services.data_recovery import artifact_data_state
from apps.workspaces.services.reconciliation import (
    STALE_JOB_THRESHOLD,
    reconcile_workspace_data_recovery,
)
from apps.workspaces.task_dispatch import adefer_recover_workspace_data

logger = logging.getLogger(__name__)

DISPATCH_FAILED_ERROR = "Failed to start data recovery"


class Admission(StrEnum):
    NOT_NEEDED = "not_needed"
    """Nothing to start: the data is usable, or a repair is already running."""
    UNRECOVERABLE = "unrecoverable"
    """The surface is broken in a way no recovery type repairs."""
    STARTED = "started"
    JOINED = "joined"
    """Lost the insert race; bound to the recovery another request started."""
    FAILED = "failed"


@dataclass(frozen=True)
class RecoveryAdmission:
    admission: Admission
    state: dict[str, Any]
    """The artifact's data state after admission; empty when admission failed."""
    recovery: WorkspaceDataRecovery | None = None


async def current_artifact_data_state(artifact: Artifact) -> dict[str, Any]:
    """Reconcile a stranded background job before reporting artifact state."""
    active_recovery = await (
        WorkspaceDataRecovery.objects.select_related("workspace")
        .filter(
            workspace=artifact.workspace,
            state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
        )
        .order_by("-created_at")
        .afirst()
    )
    if active_recovery is not None:
        age = datetime.now(active_recovery.created_at.tzinfo) - active_recovery.created_at
        await reconcile_workspace_data_recovery(
            active_recovery,
            check_stalled=age >= STALE_JOB_THRESHOLD,
        )
    return await artifact_data_state(artifact)


async def admit_artifact_recovery(artifact: Artifact, user: User) -> RecoveryAdmission:
    """Start the repair the artifact's data state asks for, unless one already runs."""
    # The recovery task re-reads the artifact from the DB, so it needs the
    # manifest persisted rather than the in-memory copy the caller derived.
    await sync_to_async(backfill_missing_semantic_query_manifest, thread_sensitive=True)(artifact)

    state = await current_artifact_data_state(artifact)
    if state["status"] in {"ready", "not_required"} and not state.get("recovery_action"):
        return RecoveryAdmission(Admission.NOT_NEEDED, state)
    if state["status"] == "recovering":
        return RecoveryAdmission(Admission.NOT_NEEDED, state)

    recovery_type = state.get("recovery_action")
    if recovery_type not in WorkspaceDataRecovery.RecoveryType.values:
        return RecoveryAdmission(Admission.UNRECOVERABLE, state)

    try:
        recovery = await WorkspaceDataRecovery.objects.acreate(
            workspace=artifact.workspace,
            requested_by=user,
            recovery_type=recovery_type,
            source_type="artifact",
            source_id=artifact.id,
        )
    except IntegrityError:
        # If two artifact pages race, bind both to the one active recovery.
        recovery = await WorkspaceDataRecovery.objects.filter(
            workspace=artifact.workspace,
            state__in=list(WorkspaceDataRecovery.ACTIVE_STATES),
        ).afirst()
        if recovery is None:
            logger.exception("Artifact recovery dedupe failed without an active row")
            return RecoveryAdmission(Admission.FAILED, {})
        return RecoveryAdmission(
            Admission.JOINED, await current_artifact_data_state(artifact), recovery
        )

    try:
        job = await adefer_recover_workspace_data(recovery_id=str(recovery.id))
        job_id = getattr(job, "id", job) if not isinstance(job, int) else job
        await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
            procrastinate_job_id=job_id
        )
        recovery.procrastinate_job_id = job_id
    except Exception as exc:
        logger.exception("Failed to dispatch artifact data recovery %s", recovery.id)
        recovery.state = WorkspaceDataRecovery.State.FAILED
        recovery.error = str(exc)[:1000] or DISPATCH_FAILED_ERROR
        await WorkspaceDataRecovery.objects.filter(id=recovery.id).aupdate(
            state=recovery.state, error=recovery.error
        )
        return RecoveryAdmission(Admission.FAILED, {}, recovery)

    return RecoveryAdmission(
        Admission.STARTED, await current_artifact_data_state(artifact), recovery
    )
