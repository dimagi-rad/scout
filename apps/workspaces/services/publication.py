"""View-schema and semantic-model publication for a workspace's loaded data.

The registered tasks in ``apps.workspaces.tasks`` are thin adapters over these;
follow-up rebuilds are queued through ``task_dispatch`` by registered name.
"""

import logging

from django.db.models import Count, OuterRef, Subquery

from apps.common.capacity import CapacityExhausted
from apps.semantic.services.cube_schema import (
    CubeSchemaBuildError,
    build_and_promote_cube_schema,
    record_cube_schema_build_deferred,
    record_cube_schema_build_failure,
)
from apps.workspaces.models import (
    SchemaState,
    Workspace,
    WorkspaceTenant,
    WorkspaceViewSchema,
)
from apps.workspaces.services.data_operation import (
    run_data_thread,
    serialized_workspace_data,
)
from apps.workspaces.services.data_operation import (
    to_thread_fresh_db as _to_thread_fresh_db,
)
from apps.workspaces.services.data_recovery import CAPACITY_REFUSED_KEY
from apps.workspaces.services.query_state import (
    included_tenant_snapshot_state as _included_tenant_snapshot_state,
)
from apps.workspaces.services.schema_manager import (
    NoActiveTenantSchema,
    SchemaManager,
    ViewSchemaRetired,
)
from apps.workspaces.task_dispatch import (
    adefer_rebuild_workspace_semantic_model,
    adefer_rebuild_workspace_view_schema,
)

logger = logging.getLogger(__name__)


def multi_tenant_count_subquery():
    """Correlated subquery yielding a workspace's total tenant count.

    A plain ``annotate(Count("workspace_tenants"))`` shares the same join as a
    ``filter(workspace_tenants__tenant_id__in=...)`` predicate, so the count
    collapses to only the *filtered* tenants (always 1 here) — the classic
    Django filter+aggregate-on-the-same-multivalued-relation trap. Counting via
    an independent subquery over the junction sidesteps that and stays a single
    SQL round-trip (no per-tenant N+1).
    """
    return Subquery(
        WorkspaceTenant.objects.filter(workspace=OuterRef("pk"))
        .order_by()
        .values("workspace")
        .annotate(n=Count("id"))
        .values("n")
    )


def _dependent_view_schema_workspaces(tenant_ids, exclude_workspace_id=None):
    """Queryset of multi-tenant workspaces with a WorkspaceViewSchema row that
    share any of ``tenant_ids``.

    A workspace qualifies when it (i) contains at least one of the given tenants,
    (ii) is multi-tenant (>= 2 tenants), and (iii) has a WorkspaceViewSchema row
    that is not retiring. A rebuild marks the row ACTIVE, so rebuilding a TEARDOWN
    or EXPIRED row would revive an idle workspace's views for another TTL (C2);
    its pending teardown already accounts for the dropped views.

    When ``exclude_workspace_id`` is given, that workspace is left
    out — used by the materialize path, which rebuilds its own view schema inline
    and only needs to fan out to the *siblings*. The refresh/teardown paths pass
    no exclusion because they are not scoped to a workspace.

    Uses a single annotated query (a subquery tenant count) rather than walking
    each tenant's workspaces, so cost is independent of the number of tenants
    materialized (no N+1).
    """
    qs = Workspace.objects.filter(
        workspace_tenants__tenant_id__in=tenant_ids,
        view_schema__isnull=False,
    ).exclude(view_schema__state__in=(SchemaState.TEARDOWN, SchemaState.EXPIRED))
    if exclude_workspace_id is not None:
        qs = qs.exclude(id=exclude_workspace_id)
    return (
        qs.annotate(num_tenants=multi_tenant_count_subquery()).filter(num_tenants__gte=2).distinct()
    )


async def rebuild_dependent_view_schemas(tenant_ids, *, exclude_workspace_id=None) -> None:
    """Defer a view-schema rebuild for every multi-tenant workspace whose
    namespaced views were (or will be) cascade-dropped by a (re-)materialization
    or refresh of one of ``tenant_ids``.

    Shared by materialize_workspace (which excludes the current workspace it
    already rebuilt inline) and the refresh/teardown paths (which exclude
    nothing). The query already yields distinct workspace ids, so no extra dedupe
    is needed. Best-effort: a failure to defer one rebuild must not block the
    caller, so each defer is individually guarded and the dispatched task owns its
    own failure handling.
    """
    async for ws_id in (
        _dependent_view_schema_workspaces(tenant_ids, exclude_workspace_id)
        .values_list("id", flat=True)
        .aiterator()
    ):
        try:
            await adefer_rebuild_workspace_view_schema(workspace_id=str(ws_id))
        except Exception:
            logger.exception(
                "Failed to defer dependent view-schema rebuild for workspace %s", ws_id
            )


async def defer_cube_promotion(workspace) -> dict:
    """Describe pending build work, even before the first semantic model exists.

    This operation outcome does not assert catalog availability; creating a
    placeholder model merely to record a deferral would change that contract.
    """
    reason = "Semantic promotion is waiting for an included source's refresh to finish."
    await _to_thread_fresh_db(record_cube_schema_build_deferred, workspace, reason)
    return {"ok": False, "status": "deferred", "reason": reason}


@serialized_workspace_data
async def rebuild_workspace_view_schema(workspace_id: str, revive_retired: bool = False) -> dict:
    """Build (or rebuild) the UNION ALL view schema for a multi-tenant workspace.

    On success: marks WorkspaceViewSchema.state = ACTIVE.
    On failure: marks state = FAILED and returns an error dict.

    A row that went TEARDOWN or EXPIRED after this job was queued is skipped: the
    retirement is the later decision, and reviving it would make its queued
    teardown abort. Whoever means to bring the views back (adding a source, an
    explicit recovery) moves the row to PROVISIONING or passes ``revive_retired``.
    """
    try:
        workspace = await Workspace.objects.prefetch_related("tenants").aget(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.exception("rebuild_workspace_view_schema: workspace %s not found", workspace_id)
        return {"error": "Workspace not found"}

    manager = SchemaManager()
    try:
        vs = await _to_thread_fresh_db(
            manager.build_view_schema, workspace, revive_retired=revive_retired
        )
    except ViewSchemaRetired as exc:
        logger.info(
            "rebuild_workspace_view_schema: skipping workspace %s — its view schema is %s",
            workspace_id,
            exc.state,
        )
        return {"status": "skipped", "reason": str(exc)}
    except Exception as exc:
        # build_view_schema owns the row state (FAILED for a first build, ACTIVE
        # plus last_error when the rolled-back views still serve), so don't
        # re-write state here and risk clobbering a concurrent transition —
        # e.g. TEARDOWN set by expire_inactive_schemas (arch #255 03#2).
        if isinstance(exc, NoActiveTenantSchema):
            logger.warning("Cannot build view schema for workspace %s: %s", workspace_id, exc)
        else:
            logger.exception("Failed to build view schema for workspace %s", workspace_id)
        skip_reason = (
            "Semantic Cube schema build skipped because the workspace view schema build failed."
        )
        await _to_thread_fresh_db(
            record_cube_schema_build_failure,
            workspace,
            skip_reason,
        )
        failed_view_schema = await WorkspaceViewSchema.objects.filter(workspace=workspace).afirst()
        tenant_coverage = (
            failed_view_schema.tenant_coverage
            if failed_view_schema is not None
            and isinstance(failed_view_schema.tenant_coverage, dict)
            else {}
        )
        return {
            "error": "Failed to build view schema",
            "tenant_coverage": tenant_coverage,
        }

    logger.info(
        "View schema '%s' is now active for workspace %s",
        vs.schema_name,
        workspace_id,
    )
    tenant_coverage = vs.tenant_coverage if isinstance(vs.tenant_coverage, dict) else {}
    snapshot_state = await _included_tenant_snapshot_state(workspace, tenant_coverage)
    if snapshot_state == "in_progress":
        return {
            "status": "active",
            "schema_name": vs.schema_name,
            "tenant_coverage": tenant_coverage,
            "cube_schema": await defer_cube_promotion(workspace),
        }
    if snapshot_state == "unsafe":
        error = "Semantic Cube schema build skipped because an included tenant snapshot is unsafe."
        await _to_thread_fresh_db(record_cube_schema_build_failure, workspace, error)
        return {
            "status": "active",
            "schema_name": vs.schema_name,
            "tenant_coverage": tenant_coverage,
            "cube_schema": {"ok": False, "error": error},
        }
    try:
        cube_schema = await run_data_thread(build_and_promote_cube_schema, workspace)
    except CubeSchemaBuildError as exc:
        logger.warning(
            "Semantic Cube schema build failed after view schema rebuild for workspace %s: %s",
            workspace_id,
            exc,
        )
        return {
            "status": "active",
            "schema_name": vs.schema_name,
            "tenant_coverage": tenant_coverage,
            "cube_schema": {"ok": False, "error": str(exc)[:500]},
        }
    except CapacityExhausted as exc:
        logger.warning(
            "Semantic Cube schema build refused at capacity after view schema rebuild for "
            "workspace %s",
            workspace_id,
        )
        return {
            "status": "active",
            "schema_name": vs.schema_name,
            "tenant_coverage": tenant_coverage,
            "cube_schema": {"ok": False, "error": str(exc)[:500], CAPACITY_REFUSED_KEY: True},
        }
    except Exception as exc:
        logger.exception(
            "Semantic Cube schema build failed after view schema rebuild for workspace %s",
            workspace_id,
        )
        return {
            "status": "active",
            "schema_name": vs.schema_name,
            "tenant_coverage": tenant_coverage,
            "cube_schema": {"ok": False, "error": str(exc)[:500]},
        }
    return {
        "status": "active",
        "schema_name": vs.schema_name,
        "tenant_coverage": tenant_coverage,
        "cube_schema": {
            "ok": True,
            "id": str(cube_schema.id),
            "content_hash": cube_schema.content_hash,
        },
    }


async def rebuild_single_tenant_semantic_models(tenant_ids) -> None:
    """Defer a semantic-model rebuild for single-tenant workspaces on ``tenant_ids``.

    Best-effort, mirroring rebuild_dependent_view_schemas: a failed defer must
    not block the caller.
    """
    qs = (
        Workspace.objects.filter(workspace_tenants__tenant_id__in=tenant_ids)
        .annotate(num_tenants=multi_tenant_count_subquery())
        .filter(num_tenants=1)
        .distinct()
    )
    async for ws_id in qs.values_list("id", flat=True).aiterator():
        try:
            await adefer_rebuild_workspace_semantic_model(workspace_id=str(ws_id))
        except Exception:
            logger.exception("Failed to defer semantic model rebuild for workspace %s", ws_id)


@serialized_workspace_data
async def rebuild_workspace_semantic_model_core(workspace_id: str) -> dict:
    """Rebuild the semantic model + Cube schema without dispatching another job."""
    try:
        workspace = await Workspace.objects.aget(id=workspace_id)
    except Workspace.DoesNotExist:
        logger.exception("rebuild_workspace_semantic_model: workspace %s not found", workspace_id)
        return {"error": "Workspace not found"}
    if await workspace.tenants.acount() > 1:
        return await rebuild_workspace_view_schema(workspace_id)
    snapshot_state = await _included_tenant_snapshot_state(workspace, None)
    if snapshot_state == "in_progress":
        return {"cube_schema": await defer_cube_promotion(workspace)}
    if snapshot_state == "unsafe":
        error = "Semantic Cube schema build skipped because an included tenant snapshot is unsafe."
        await _to_thread_fresh_db(record_cube_schema_build_failure, workspace, error)
        return {"cube_schema": {"ok": False, "error": error}}
    try:
        cube_schema = await run_data_thread(build_and_promote_cube_schema, workspace)
    except CubeSchemaBuildError as exc:
        logger.warning("Semantic model rebuild failed for workspace %s: %s", workspace_id, exc)
        return {"cube_schema": {"ok": False, "error": str(exc)[:500]}}
    except CapacityExhausted as exc:
        logger.warning("Semantic model rebuild refused at capacity for workspace %s", workspace_id)
        return {"cube_schema": {"ok": False, "error": str(exc)[:500], CAPACITY_REFUSED_KEY: True}}
    except Exception as exc:
        logger.exception("Semantic model rebuild failed for workspace %s", workspace_id)
        return {"cube_schema": {"ok": False, "error": str(exc)[:500]}}
    return {
        "cube_schema": {
            "ok": True,
            "id": str(cube_schema.id),
            "content_hash": cube_schema.content_hash,
        }
    }
