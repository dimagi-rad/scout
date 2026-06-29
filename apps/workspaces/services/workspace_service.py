"""Service functions for workspace tenant management."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.workspaces.models import SchemaState, WorkspaceTenant, WorkspaceViewSchema
from apps.workspaces.tasks import rebuild_workspace_view_schema, teardown_view_schema_task


def add_workspace_tenant(workspace, tenant) -> tuple[WorkspaceTenant, bool]:
    """Add a tenant to a workspace and mark the view schema for rebuild.

    Uses get_or_create to atomically handle concurrent requests. Only triggers
    the schema rebuild when a new WorkspaceTenant is actually created.

    Returns (WorkspaceTenant, created) where created is False if the tenant
    was already in the workspace.
    """
    with transaction.atomic():
        wt, created = WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)
        if created:
            WorkspaceViewSchema.objects.filter(workspace=workspace).update(
                state=SchemaState.PROVISIONING
            )
            rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))

    return wt, created


def remove_workspace_tenant(workspace, wt: WorkspaceTenant) -> None:
    """Remove a tenant from a workspace and reconcile the view schema.

    Deletes the WorkspaceTenant record. If the workspace remains multi-tenant
    (>=2 tenants left), marks any existing WorkspaceViewSchema as PROVISIONING
    and dispatches a rebuild. If the workspace drops to single-tenant (or zero),
    routing moves to the tenant schema and any active view schema becomes an
    orphan — mark it TEARDOWN and dispatch teardown so the physical
    ``ws_<hash>`` schema is dropped.

    Both ``defer`` calls are transaction-safe — the procrastinate row is only
    visible to workers after commit.

    Raises ValidationError if wt is the last tenant in the workspace.
    """
    with transaction.atomic():
        # Lock tenant rows before counting so concurrent removals can't both pass
        # the last-tenant guard. Evaluate to a list because PostgreSQL forbids
        # FOR UPDATE with aggregates (.count()).
        tenant_ids = list(
            workspace.workspace_tenants.select_for_update().values_list("id", flat=True)
        )
        if len(tenant_ids) <= 1:
            raise ValidationError("Cannot remove the last tenant from a workspace.")
        wt.delete()
        remaining = len(tenant_ids) - 1
        if remaining <= 1:
            for vs in WorkspaceViewSchema.objects.filter(
                workspace=workspace, state=SchemaState.ACTIVE
            ):
                vs.state = SchemaState.TEARDOWN
                vs.save(update_fields=["state"])
                teardown_view_schema_task.defer(view_schema_id=str(vs.id))
        else:
            WorkspaceViewSchema.objects.filter(workspace=workspace).update(
                state=SchemaState.PROVISIONING
            )
            rebuild_workspace_view_schema.defer(workspace_id=str(workspace.id))


async def touch_workspace_schemas(workspace) -> None:
    """Reset the inactivity TTL for a workspace's active schemas.

    Multi-tenant: touches the WorkspaceViewSchema *and* every constituent
    TenantSchema — chat activity never touches the underlying schemas directly,
    so without this they expire and their DROP CASCADE destroys the views inside
    the still-ACTIVE view schema.
    """
    from apps.workspaces.models import TenantSchema

    tenant_count = await workspace.workspace_tenants.acount()
    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        ts = await TenantSchema.objects.filter(
            tenant=tenant,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if ts is not None:
            await ts.atouch()
    elif tenant_count > 1:
        # Touch tenant schemas even if no view schema row exists — they underpin it.
        tenant_ids = [t.id async for t in workspace.tenants.all()]
        await TenantSchema.objects.filter(
            tenant_id__in=tenant_ids,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).aupdate(last_accessed_at=timezone.now())

        vs = await WorkspaceViewSchema.objects.filter(
            workspace=workspace,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if vs is not None:
            await vs.atouch()
