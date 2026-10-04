"""Whether a workspace has data to serve, and whether a load of it is under way.

Kept free of ``apps.workspaces.tasks`` so the agent prompt can ask too: the tasks
module imports the agent graph.
"""

import uuid
from collections import defaultdict
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
    WorkspaceViewSchema,
)
from apps.workspaces.services.status import workspace_schema_status
from apps.workspaces.task_dispatch import (
    MATERIALIZE_WORKSPACE,
    REBUILD_WORKSPACE_SEMANTIC_MODEL,
    REBUILD_WORKSPACE_VIEW_SCHEMA,
)

MATERIALIZE_TASK_NAME = MATERIALIZE_WORKSPACE
REBUILD_VIEW_TASK_NAME = REBUILD_WORKSPACE_VIEW_SCHEMA
# A view rebuild queued on its own (a source added to a serving workspace) is a
# build in flight too, so the status counts it; the chat auto-load does not wait on it.
_STATUS_TASK_NAMES = (MATERIALIZE_TASK_NAME, REBUILD_VIEW_TASK_NAME)
REBUILD_SEMANTIC_TASK_NAME = REBUILD_WORKSPACE_SEMANTIC_MODEL
# Everything that must finish before a held request can be answered from the data.
_BUILD_TASK_NAMES = (*_STATUS_TASK_NAMES, REBUILD_SEMANTIC_TASK_NAME)
_QUEUED_OR_RUNNING = ("todo", "doing", "aborting")
_STARTED = ("doing", "aborting")
# Matches MATERIALIZATION_STALLED_HEARTBEAT_SECONDS in reconciliation: a started job whose
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


async def aworkspace_serves_nothing(workspace_id) -> bool:
    """Whether the workspace has sources and none of them serves data yet."""
    unserved = await aunserved_tenant_ids(workspace_id)
    return bool(unserved) and (
        len(unserved) >= await WorkspaceTenant.objects.filter(workspace_id=workspace_id).acount()
    )


def active_runs_for_workspaces(workspace_ids):
    """Unevaluated: the active runs on any tenant of these workspaces, owned or not.

    Tenants are shared, so a sibling's run counts here; ``owned_run_q`` narrows to the
    workspace's own. The join also lets a caller read the workspace id per run; with
    several ids a run on a tenant shared by two of them yields one row per
    (run, workspace), so collapse to a set rather than counting rows.
    """
    return MaterializationRun.objects.filter(
        tenant_schema__tenant__workspace_tenants__workspace_id__in=[
            str(workspace_id) for workspace_id in workspace_ids
        ],
        state__in=list(MaterializationRun.ACTIVE_STATES),
    )


def owned_run_q(workspace) -> Q:
    """Runs this workspace started, as opposed to a sibling's load of a shared tenant."""
    workspace_job_ids = ThreadJob.objects.filter(
        thread__workspace_id=workspace.id, job_type=ThreadJob.JobType.MATERIALIZATION
    ).values("procrastinate_job_id")
    # A load candidate carries load_workspace_id only until promotion, which is
    # after its run finishes; finished runs are found through the job id instead.
    return (
        Q(tenant_schema__load_workspace_id=workspace.id)
        | Q(
            tenant_schema__refresh_workspace_id=workspace.id,
            tenant_schema__state=SchemaState.PROVISIONING,
        )
        | Q(procrastinate_job_id__in=workspace_job_ids)
    )


def _pending_loads(workspace_ids, task_names=(MATERIALIZE_TASK_NAME,)):
    """The run, recovery and queued-job querysets that each mean a load is under way.

    A MaterializationRun only exists once a worker has started the job, so a
    queued job of one of *task_names* is read from the queue itself.
    """
    ids = [str(workspace_id) for workspace_id in workspace_ids]
    runs = active_runs_for_workspaces(ids)
    recoveries = WorkspaceDataRecovery.objects.filter(
        workspace_id__in=ids, state__in=list(WorkspaceDataRecovery.ACTIVE_STATES)
    )
    stalled_before = timezone.now() - _STALLED_AFTER
    jobs = ProcrastinateJob.objects.filter(
        task_name__in=task_names,
        status__in=_QUEUED_OR_RUNNING,
        args__workspace_id__in=ids,
    ).filter(~Q(status__in=_STARTED) | Q(worker__last_heartbeat__gte=stalled_before))
    return runs, recoveries, jobs


def workspace_build_pending(workspace_id) -> bool:
    """``aworkspace_build_pending`` for sync callers."""
    return any(pending.exists() for pending in _pending_loads([workspace_id], _BUILD_TASK_NAMES))


async def aworkspace_load_pending(workspace_id) -> bool:
    """Whether a load covering this workspace is queued or running."""
    return await _aany_pending(_pending_loads([workspace_id]))


async def aworkspace_own_load_pending(workspace) -> bool:
    """Whether a load of this workspace itself is queued or running.

    Unlike ``aworkspace_load_pending``, a sibling workspace's run on a shared
    tenant does not count: it builds that workspace's catalog, not this one's,
    and its end flushes nothing here. The workspace's own runs (a refresh, a
    recipe's inline load) do.
    """
    runs, recoveries, jobs = _pending_loads([workspace.id])
    return await _aany_pending([runs.filter(owned_run_q(workspace)), recoveries, jobs])


async def aworkspace_build_pending(workspace_id) -> bool:
    """Whether a load, or a view or semantic-model rebuild after one, is queued or running."""
    return await _aany_pending(_pending_loads([workspace_id], _BUILD_TASK_NAMES))


async def _aany_pending(querysets) -> bool:
    for pending in querysets:
        if await pending.aexists():
            return True
    return False


def _workspace_ids_building(workspace_ids) -> set:
    """Of ``workspace_ids``, those with a load or view build queued or running (three queries)."""
    workspace_ids = list(workspace_ids)
    runs, recoveries, jobs = _pending_loads(workspace_ids, _STATUS_TASK_NAMES)
    pending = {
        str(workspace_id)
        for workspace_id in runs.values_list(
            "tenant_schema__tenant__workspace_tenants__workspace_id", flat=True
        )
    }
    pending |= {
        str(workspace_id) for workspace_id in recoveries.values_list("workspace_id", flat=True)
    }
    pending |= {
        str(workspace_id) for workspace_id in jobs.values_list("args__workspace_id", flat=True)
    }
    return {workspace_id for workspace_id in workspace_ids if str(workspace_id) in pending}


def workspace_schema_statuses(workspace_ids) -> dict:
    """``schema_status`` per workspace id, in a fixed number of queries however many."""
    workspace_ids = list(workspace_ids)
    if not workspace_ids:
        return {}
    tenants_by_workspace = defaultdict(set)
    for workspace_id, tenant_id in WorkspaceTenant.objects.filter(
        workspace_id__in=workspace_ids
    ).values_list("workspace_id", "tenant_id"):
        tenants_by_workspace[workspace_id].add(tenant_id)
    all_tenant_ids = set().union(*tenants_by_workspace.values())
    active = set(
        TenantSchema.objects.filter(
            tenant_id__in=all_tenant_ids, state=SchemaState.ACTIVE
        ).values_list("tenant_id", flat=True)
    )
    view_states = dict(
        WorkspaceViewSchema.objects.filter(workspace_id__in=workspace_ids).values_list(
            "workspace_id", "state"
        )
    )
    loading = _workspace_ids_building(workspace_ids)
    return {
        workspace_id: workspace_schema_status(
            tenants_by_workspace[workspace_id],
            active,
            workspace_id in loading,
            view_states.get(workspace_id),
        )
        for workspace_id in workspace_ids
    }


async def aworkspace_schema_status(workspace_id) -> str:
    """``workspace_schema_statuses`` for one workspace, from async code."""
    tenant_ids = {
        tenant_id
        async for tenant_id in WorkspaceTenant.objects.filter(
            workspace_id=workspace_id
        ).values_list("tenant_id", flat=True)
    }
    active = {
        tenant_id
        async for tenant_id in TenantSchema.objects.filter(
            tenant_id__in=tenant_ids, state=SchemaState.ACTIVE
        ).values_list("tenant_id", flat=True)
    }
    view_state = (
        await WorkspaceViewSchema.objects.filter(workspace_id=workspace_id)
        .values_list("state", flat=True)
        .afirst()
    )
    building = await _aany_pending(_pending_loads([workspace_id], _STATUS_TASK_NAMES))
    return workspace_schema_status(tenant_ids, active, building, view_state)


async def athread_awaits_load(thread_id) -> bool:
    """Whether this chat has a load queued for it that will resume it when done.

    PENDING only. A job turns RUNNING when its resume claims it, after loading has
    finished and while holding the thread's turn lease, so no chat turn builds a
    prompt then; counting it would only promise a continuation for another load.
    A CANCELLED job's resume reports the cancellation rather than answering.
    """
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
