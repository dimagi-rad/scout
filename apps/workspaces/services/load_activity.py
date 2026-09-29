"""Whether a workspace has data to serve, and whether a load of it is under way.

Kept free of ``apps.workspaces.tasks`` so the agent prompt can ask too: the tasks
module imports the agent graph.
"""

import uuid
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.chat.models import ThreadJob
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceDataRecovery,
    WorkspaceTenant,
)

MATERIALIZE_TASK_NAME = "apps.workspaces.tasks.materialize_workspace"
_QUEUED_OR_RUNNING = ("todo", "doing", "aborting")
_STARTED = ("doing", "aborting")
# Matches MATERIALIZATION_STALLED_HEARTBEAT_SECONDS in tasks: a started job whose
# worker stopped heart-beating is dead and must not hold off every later load.
_STALLED_AFTER = timedelta(seconds=300)


async def aunserved_tenant_ids(workspace_id) -> set:
    """The workspace's sources with no ACTIVE schema, so no data to answer from."""
    tenant_ids = {
        tenant_id
        async for tenant_id in WorkspaceTenant.objects.filter(
            workspace_id=workspace_id
        ).values_list("tenant_id", flat=True)
    }
    served = {
        tenant_id
        async for tenant_id in TenantSchema.objects.filter(
            tenant_id__in=tenant_ids, state=SchemaState.ACTIVE
        ).values_list("tenant_id", flat=True)
    }
    return tenant_ids - served


async def aworkspace_load_pending(workspace_id) -> bool:
    """Whether a load covering this workspace is queued or running.

    A MaterializationRun only exists once a worker has started the job, so a
    queued ``materialize_workspace`` job is read from the queue itself.
    """
    if await MaterializationRun.objects.filter(
        tenant_schema__tenant__workspace_tenants__workspace_id=workspace_id,
        state__in=list(MaterializationRun.ACTIVE_STATES),
    ).aexists():
        return True
    if await WorkspaceDataRecovery.objects.filter(
        workspace_id=workspace_id, state__in=list(WorkspaceDataRecovery.ACTIVE_STATES)
    ).aexists():
        return True
    stalled_before = timezone.now() - _STALLED_AFTER
    return await (
        ProcrastinateJob.objects.filter(
            task_name=MATERIALIZE_TASK_NAME,
            status__in=_QUEUED_OR_RUNNING,
            args__workspace_id=str(workspace_id),
        )
        .filter(~Q(status__in=_STARTED) | Q(worker__last_heartbeat__gte=stalled_before))
        .aexists()
    )


async def athread_awaits_load(thread_id) -> bool:
    """Whether this chat has a load queued for it that will resume it when done."""
    try:
        thread_id = uuid.UUID(str(thread_id))
    except ValueError:
        # Synthetic ids (a recipe run's) name no Thread, so nothing is bound to them.
        return False
    return await ThreadJob.objects.filter(
        thread_id=thread_id,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        state=ThreadJob.State.PENDING,
    ).aexists()
