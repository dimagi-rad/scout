"""Canonical world-state read-model for a workspace (arch #251).

Every surface that needs to answer "is this workspace's data loaded, and is
anything in flight" should derive it here, so the status API, prompt builders,
MCP tools, and the DRF dictionary can never drift apart. This is a READ-ONLY
derivation: it observes SchemaState / RunState / WorkspaceViewSchema but never
writes them (those substrates are owned elsewhere).

Phase 1 (this module) is adopted by the workspace status API only; later phases
adopt it in the prompt builders and MCP get_schema_status.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceTenant,
    WorkspaceViewSchema,
)

WorldStatus = Literal["available", "provisioning", "unavailable", "failed"]

# A run that reached one of these states loaded data (COMPLETED = all sources,
# PARTIAL = some), so both count toward last_synced_at (arch #251 Decision 3).
_SYNCED_RUN_STATES = (
    MaterializationRun.RunState.COMPLETED,
    MaterializationRun.RunState.PARTIAL,
)

# View-schema states that mean the multi-tenant query layer is mid-(re)build:
# build_view_schema parks the row in PROVISIONING while it runs, and
# rebuild_view_schema moves it through TEARDOWN first.
_VIEW_REBUILD_STATES = (SchemaState.PROVISIONING, SchemaState.TEARDOWN)


@dataclass
class WorldState:
    status: WorldStatus
    in_progress: bool
    last_synced_at: datetime | None
    last_error: str | None
    is_multi_tenant: bool


def _derive_status(tenant_count, active_count, provisioning, view_schema_state) -> WorldStatus:
    """The workspace's coarse schema status. Shared oracle so list/detail and any
    later consumer never disagree.

    - Single-tenant: available iff every tenant is ACTIVE; provisioning if any is
      mid-provisioning; else unavailable.
    - Multi-tenant: tracked by the view schema — ACTIVE ⇒ available, FAILED ⇒
      failed (per-tenant data may have loaded but there's no queryable surface),
      else provisioning.
    """
    if tenant_count > 1:
        if view_schema_state == SchemaState.ACTIVE:
            return "available"
        if view_schema_state == SchemaState.FAILED:
            return "failed"
        return "provisioning"

    if active_count == tenant_count and tenant_count > 0:
        return "available"
    if provisioning:
        return "provisioning"
    return "unavailable"


async def derive_world_state(workspace) -> WorldState:
    """Derive the canonical WorldState for ``workspace`` using async ORM only.

    Self-contained (does its own queries rather than relying on prefetch) so it
    can be called from any async context, or per-workspace via ``async_to_sync``
    from the sync DRF views.
    """
    tenant_ids = [
        tid
        async for tid in WorkspaceTenant.objects.filter(workspace=workspace).values_list(
            "tenant_id", flat=True
        )
    ]
    tenant_count = len(tenant_ids)
    is_multi_tenant = tenant_count > 1

    active_tenant_ids: set = set()
    provisioning = False
    if tenant_ids:
        async for tenant_id, state in TenantSchema.objects.filter(
            tenant_id__in=tenant_ids
        ).values_list("tenant_id", "state"):
            if state == SchemaState.ACTIVE:
                active_tenant_ids.add(tenant_id)
            elif state in (SchemaState.PROVISIONING, SchemaState.MATERIALIZING):
                provisioning = True

    view_schema = None
    if is_multi_tenant:
        view_schema = await WorkspaceViewSchema.objects.filter(workspace=workspace).afirst()
    view_schema_state = view_schema.state if view_schema else None

    status = _derive_status(
        tenant_count=tenant_count,
        active_count=len(active_tenant_ids),
        provisioning=provisioning,
        view_schema_state=view_schema_state,
    )

    last_synced_at = None
    in_progress = False
    if tenant_ids:
        last_synced_at = (
            await MaterializationRun.objects.filter(
                state__in=_SYNCED_RUN_STATES,
                completed_at__isnull=False,
                tenant_schema__tenant_id__in=tenant_ids,
            )
            .order_by("-completed_at")
            .values_list("completed_at", flat=True)
            .afirst()
        )
        in_progress = await MaterializationRun.objects.filter(
            state__in=MaterializationRun.ACTIVE_STATES,
            tenant_schema__tenant_id__in=tenant_ids,
        ).aexists()

    if is_multi_tenant and view_schema_state in _VIEW_REBUILD_STATES:
        in_progress = True

    return WorldState(
        status=status,
        in_progress=in_progress,
        last_synced_at=last_synced_at,
        last_error=(view_schema.last_error or None) if view_schema else None,
        is_multi_tenant=is_multi_tenant,
    )
