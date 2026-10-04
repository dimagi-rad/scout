"""What a finished materialization job did, per workspace tenant.

Read from the job's MaterializationRun rows and the workspace's tenant list, so a
consumer (the chat resume) gets a completed load description, not pipeline state.
"""

import logging
import uuid

from django.db.models import Q

from apps.common.error_codes import ErrorCode
from apps.transformations.models import TransformationRunStatus
from apps.users.models import TenantMembership
from apps.workspaces.models import MaterializationRun, SchemaState, WorkspaceTenant
from apps.workspaces.services.failure_guidance import compose_failure_summary

logger = logging.getLogger(__name__)


def set_tenant_display_names(summaries: list[dict]) -> None:
    providers_by_name: dict[str, set[str]] = {}
    for entry in summaries:
        providers_by_name.setdefault(entry["tenant"], set()).add(entry.get("provider", ""))
    for entry in summaries:
        if len(providers_by_name[entry["tenant"]]) > 1 and entry.get("provider"):
            entry["display_name"] = f"{entry['tenant']} ({entry['provider']})"


def unreachable_tenant_error(tenant) -> str:
    """Describe a workspace tenant the acting user holds no live membership for.

    Describes only. What the user should do about it is keyed by
    ``WORKSPACE_TENANT_UNREACHABLE`` in ``CREDENTIAL_GUIDANCE``, because the
    run summary and the chat resume both report this and neither should phrase
    the advice itself (see ``apps/common/errors.py``).
    """
    return (
        f"no live {tenant.provider} membership for the acting user on "
        f"'{tenant.external_id}', so this tenant was not attempted"
    )


TENANT_NOT_RUN = "not_run"
"""Summary ``state`` for a workspace tenant this job produced no run row for.

Deliberately not a ``MaterializationRun.RunState`` — there is no run to have a
state. Consumers must not treat it as one.
"""


async def _uncovered_tenant_summaries(
    workspace, user_id: str, covered_tenant_ids: set, preflight_failures: list[dict] | None = None
) -> list[dict]:
    """Summary entries for workspace tenants this job produced no run row for.

    ``MaterializationRun`` rows are only created from inside ``run_pipeline``, so
    every tenant dropped before that point — unreachable for the acting user, no
    pipeline for its provider, credential refused pre-flight — leaves no row at
    all. Run rows can say what loaded but never what was missed, so the
    workspace's tenant list is the only thing that can (#364).

    Recorded preflight failures preserve what this job knew before any run was
    created. Older jobs have no record: membership can establish lack of access,
    but cannot explain other missing runs, so those carry no inferred advice.
    """
    uncovered = [
        wt.tenant
        async for wt in WorkspaceTenant.objects.filter(workspace=workspace)
        .exclude(tenant_id__in=covered_tenant_ids)
        .select_related("tenant")
    ]
    if not uncovered:
        return []
    reachable: set = set()
    if user_id:
        reachable = {
            tenant_id
            async for tenant_id in TenantMembership.objects.filter(
                user_id=user_id,
                archived_at__isnull=True,
                tenant_id__in=[t.id for t in uncovered],
            ).values_list("tenant_id", flat=True)
        }
    # Provider-scoped failures must not advise against a replacement provider's credentials.
    recorded_failures = {
        (entry.get("tenant_id"), entry.get("provider")): entry
        for entry in preflight_failures or []
        if isinstance(entry, dict) and entry.get("error")
    }
    summaries = []
    for tenant in uncovered:
        recorded = recorded_failures.get((str(tenant.id), tenant.provider))
        if recorded:
            error = str(recorded["error"])
            code = str(recorded.get("error_code") or ErrorCode.INTERNAL_ERROR)
        elif tenant.id in reachable:
            error = (
                f"this workspace includes {tenant.provider} source "
                f"'{tenant.external_id}' but the run recorded nothing for it"
            )
            code = ErrorCode.INTERNAL_ERROR
        else:
            error = unreachable_tenant_error(tenant)
            code = ErrorCode.WORKSPACE_TENANT_UNREACHABLE
        summaries.append(
            {
                "tenant": tenant.external_id,
                "state": TENANT_NOT_RUN,
                "materialized_row_counts": {},
                "sources": {},
                "error": error,
                "error_code": str(code),
                "preflight_recorded": bool(recorded),
                "provider": tenant.provider,
            }
        )
        logger.warning(
            "resume: workspace %s tenant %s has no materialization run for this "
            "job (%s); reporting it as not loaded (#364)",
            workspace.id,
            tenant.external_id,
            code,
        )
    return summaries


def _reused_run_ids(recorded: list[dict] | None) -> list[uuid.UUID]:
    ids = []
    for entry in recorded or []:
        if not isinstance(entry, dict):
            continue
        try:
            ids.append(uuid.UUID(str(entry.get("reused_run_id"))))
        except ValueError:
            continue
    return ids


_UNPUBLISHED_SCHEMA_STATES = frozenset(
    {SchemaState.PROVISIONING, SchemaState.FAILED, SchemaState.EXPIRED}
)


async def aggregate_materialization_state(
    procrastinate_job_id: int,
    workspace,
    user_id: str,
    preflight_failures: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Inspect MaterializationRun rows for this job, return (status, per-tenant summary).

    Per-tenant summary entries include per-source detail so the resume prompt
    can tell the agent which sources are queryable and which are unavailable:

    ``{
        "tenant": "...",
        "state": "partial",          # the MaterializationRun state
        "materialized_row_counts": {"users": 100, ...},  # only sources that committed
        "sources": {
            "users":   {"state": "completed", "rows": 100},
            "visits":  {"state": "completed", "rows": 98869},
            "completed_works": {"state": "failed",  "rows": 0,
                                "error": "ConnectionError: 500 ...",
                                "error_code": "INTERNAL_ERROR"},
            "payments": {"state": "skipped", "rows": 0},
            ...
        },
    }``

    ``workspace`` and ``user_id`` are required, not optional, because the run
    rows alone cannot see a tenant that never produced one: they made the status
    read ``completed`` for a run that covered part of the workspace (#364). Those
    tenants appear in the summary with ``state=TENANT_NOT_RUN`` and hold the
    status to ``partial`` at best.
    """
    runs = [
        r
        async for r in MaterializationRun.objects.filter(
            Q(procrastinate_job_id=procrastinate_job_id)
            | Q(
                id__in=_reused_run_ids(preflight_failures),
                tenant_schema__tenant__workspace_tenants__workspace=workspace,
            )
        )
        .select_related("tenant_schema__tenant")
        .distinct()
    ]
    uncovered = await _uncovered_tenant_summaries(
        workspace,
        user_id,
        {r.tenant_schema.tenant_id for r in runs},
        preflight_failures,
    )
    if not runs:
        set_tenant_display_names(uncovered)
        return "no_runs", uncovered
    summary: list[dict] = []
    any_cancelled = False
    any_failed = False
    all_completed = True
    for r in runs:
        tenant_id = r.tenant_schema.tenant.external_id
        # A load writes a candidate; its rows serve only once promoted. A failed
        # candidate keeps them for a resume, but nothing can query them.
        unpublished = (
            r.tenant_schema.load_workspace_id is not None
            and r.tenant_schema.state in _UNPUBLISHED_SCHEMA_STATES
        )
        materialized_row_counts: dict = {}
        sources_detail: dict = {}
        transform_error: str | None = None
        transform_test_failures: str | None = None
        run_error: str | None = None
        run_error_code: str | None = None
        if isinstance(r.result, dict):
            if r.result.get("error"):
                run_error = str(r.result["error"])
                # Carried so a run-level failure can reach the same code-keyed
                # guidance a per-source failure gets; only "sources" ever could.
                run_error_code = str(r.result.get("error_code") or "") or None
            for source, info in (r.result.get("sources") or {}).items():
                if not isinstance(info, dict):
                    continue
                src_state = info.get("state")
                if unpublished and src_state == "completed":
                    src_state = "not_published"
                if src_state == "completed" and "rows" in info:
                    materialized_row_counts[source] = info["rows"]
                detail = {"state": src_state, "rows": info.get("rows", 0)}
                if "error" in info:
                    detail["error"] = info["error"]
                if "error_code" in info:
                    detail["error_code"] = info["error_code"]
                # Expose cursor_state.last_id so the resume prompt can tell the
                # agent where a partial load will continue from (issue #187).
                cursor_state = info.get("cursor_state")
                if isinstance(cursor_state, dict) and isinstance(cursor_state.get("last_id"), int):
                    detail["resume_last_id"] = cursor_state["last_id"]
                sources_detail[source] = detail
            # Surface a failed transform phase (issue #241, 04#4): run state stays
            # COMPLETED (transform failures are isolated from the raw load), but
            # staging/derived tables are stale and the agent must disclose that.
            # TESTS_FAILED goes to its own key, never transform_error: the tables
            # built and hold data, so "transforms failed" prose would make the
            # agent disown data that is present (#391).
            transforms = r.result.get("transforms")
            if isinstance(transforms, dict):
                if transforms.get("status") == TransformationRunStatus.FAILED:
                    transform_error = transforms.get("error") or "transform phase failed"
                elif transforms.get("status") == TransformationRunStatus.TESTS_FAILED:
                    transform_test_failures = transforms.get("error") or "dbt tests failed"
                elif transforms.get("error"):
                    transform_error = transforms["error"]
        tenant_summary = {
            "tenant": tenant_id,
            "provider": r.tenant_schema.tenant.provider,
            "state": r.state,
            "materialized_row_counts": materialized_row_counts,
            "sources": sources_detail,
        }
        if transform_error:
            tenant_summary["transform_error"] = transform_error
        if transform_test_failures:
            tenant_summary["transform_test_failures"] = transform_test_failures
        if run_error:
            tenant_summary["error"] = run_error
        if run_error_code:
            tenant_summary["error_code"] = run_error_code
        if unpublished:
            tenant_summary["published"] = False
            all_completed = False
        summary.append(tenant_summary)
        if r.state == MaterializationRun.RunState.CANCELLED:
            any_cancelled = True
            all_completed = False
        elif r.state == MaterializationRun.RunState.FAILED:
            any_failed = True
            all_completed = False
        elif r.state != MaterializationRun.RunState.COMPLETED:
            all_completed = False
    summary.extend(uncovered)
    set_tenant_display_names(summary)
    if any_cancelled:
        status = "cancelled"
    elif any_failed:
        status = "failed"
    elif all_completed and not uncovered:
        status = "completed"
    else:
        # A PARTIAL run, runs still in flight (LOADING/TRANSFORMING), or a
        # workspace tenant with no run row at all — partial so the agent does
        # not falsely claim "all data loaded".
        status = "partial"
    return status, summary


async def build_failure_summary_for_job(procrastinate_job_id: int) -> str:
    """Read MaterializationRuns for this job and compose a user-facing summary."""
    runs = [
        r
        async for r in MaterializationRun.objects.filter(
            procrastinate_job_id=procrastinate_job_id,
        )
    ]
    return compose_failure_summary(runs)
