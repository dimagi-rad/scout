"""Which workspaces are loading, and how far along each source is.

Shared by the workspace payloads and the workspace-scoped active-jobs endpoint so
every member sees a load, not just the one who started it (#369, #411).
"""

from procrastinate.contrib.django.models import ProcrastinateJob

from apps.workspaces.models import (
    MaterializationRun,
    Workspace,
    WorkspaceTenant,
)
from apps.workspaces.services.load_activity import (
    active_runs_for_workspaces,
    aunserved_tenant_ids,
    owned_run_q,
)


def workspace_ids_in_progress(workspace_ids) -> set:
    """Of ``workspace_ids``, those with a run active on any of their tenants (one query)."""
    wanted = set(workspace_ids)
    if not wanted:
        return set()
    active = active_runs_for_workspaces(wanted).values_list(
        "tenant_schema__tenant__workspace_tenants__workspace_id", flat=True
    )
    return set(active) & wanted


def progress_payload(progress: dict | None) -> dict | None:
    if not progress:
        return None
    rows_loaded = progress.get("rows_loaded") or 0
    rows_total = progress.get("rows_total")
    percent = None
    if isinstance(rows_total, int) and rows_total > 0:
        percent = min(100, int(100 * rows_loaded / rows_total))
    return {
        "percent": percent,
        "rows_loaded": rows_loaded,
        "rows_total": rows_total,
        # OCS reports per-session as "sessions".
        "unit": progress.get("unit") or "rows",
        "message": progress.get("message"),
        "source": progress.get("source"),
        "step": progress.get("step"),
        "total_steps": progress.get("total_steps"),
        # A named step after the sources load (``LoadPhase``); None while loading.
        "phase": progress.get("phase"),
    }


async def aworkspace_load_progress(workspace: Workspace) -> list[dict]:
    """The active run of each load this workspace started, whoever started it.

    A workspace load runs its sources one after another under one procrastinate
    job id, so a run's position among that job's runs is "source i of N". Tenants
    are shared, so only runs this workspace started count (a sibling's load of a
    shared tenant is not this workspace's progress). Runs that never had a
    ThreadJob (``POST /refresh/``) are found through the schema's workspace
    markers instead.
    """
    tenants = {
        wt.tenant_id: wt.tenant
        async for wt in WorkspaceTenant.objects.filter(workspace_id=workspace.id).select_related(
            "tenant"
        )
    }
    if not tenants:
        return []
    active = [
        run
        async for run in active_runs_for_workspaces([workspace.id])
        .filter(owned_run_q(workspace))
        .select_related("tenant_schema")
        .order_by("started_at")
    ]
    if not active:
        return []

    job_ids = {run.procrastinate_job_id for run in active} - {None}
    run_ids_by_job: dict[int, list] = {}
    tenant_ids_by_job: dict[int, set] = {}
    async for run_id, job_id, tenant_id in (
        MaterializationRun.objects.filter(
            tenant_schema__tenant_id__in=list(tenants), procrastinate_job_id__in=job_ids
        )
        .order_by("started_at", "id")
        .values_list("id", "procrastinate_job_id", "tenant_schema__tenant_id")
    ):
        run_ids_by_job.setdefault(job_id, []).append(run_id)
        tenant_ids_by_job.setdefault(job_id, set()).add(tenant_id)

    # Adding a source loads only the unserved ones, so the denominator is not
    # every tenant of the workspace.
    only_unserved_jobs = {
        job_id
        async for job_id in ProcrastinateJob.objects.filter(
            id__in=job_ids, args__only_unserved=True
        ).values_list("id", flat=True)
    }
    unserved = await aunserved_tenant_ids(workspace.id) if only_unserved_jobs else set()

    sequenced_jobs = {
        run.procrastinate_job_id
        for run in active
        if run.procrastinate_job_id is not None
        and run.tenant_schema.load_workspace_id == workspace.id
    }
    loads = []
    for run in active:
        job_id = run.procrastinate_job_id
        sequenced = job_id in sequenced_jobs
        position = run_ids_by_job[job_id].index(run.id) + 1 if sequenced else 1
        if job_id in only_unserved_jobs:
            total = len(tenant_ids_by_job[job_id] | unserved)
        else:
            total = len(tenants)
        tenant = tenants[run.tenant_schema.tenant_id]
        loads.append(
            {
                "procrastinate_job_id": job_id,
                "tenant_id": str(tenant.id),
                "tenant_name": tenant.canonical_name or tenant.external_id,
                "source_index": position,
                "source_total": max(total, position) if sequenced else 1,
                "state": run.state,
                "started_at": run.started_at.isoformat(),
                "progress": progress_payload(run.progress),
            }
        )
    return loads
