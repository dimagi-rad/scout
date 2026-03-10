"""Service functions for workspace tenant management."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.projects.models import SchemaState, WorkspaceTenant, WorkspaceViewSchema


def add_workspace_tenant(workspace, tenant) -> tuple[WorkspaceTenant, bool]:
    """Add a tenant to a workspace and mark the view schema for rebuild.

    Uses get_or_create to atomically handle concurrent requests. Only triggers
    the schema rebuild when a new WorkspaceTenant is actually created.

    Returns (WorkspaceTenant, created) where created is False if the tenant
    was already in the workspace.
    """
    from apps.projects.tasks import rebuild_workspace_view_schema

    with transaction.atomic():
        wt, created = WorkspaceTenant.objects.get_or_create(workspace=workspace, tenant=tenant)
        if created:
            WorkspaceViewSchema.objects.filter(workspace=workspace).update(
                state=SchemaState.PROVISIONING
            )
            rebuild_workspace_view_schema.delay_on_commit(str(workspace.id))

    return wt, created


def remove_workspace_tenant(workspace, wt: WorkspaceTenant) -> None:
    """Remove a tenant from a workspace and mark the view schema for rebuild.

    Deletes the WorkspaceTenant record and marks any existing WorkspaceViewSchema
    as PROVISIONING, then dispatches a rebuild task after the transaction commits.

    Raises ValidationError if wt is the last tenant in the workspace.
    """
    from apps.projects.tasks import rebuild_workspace_view_schema

    with transaction.atomic():
        # Lock all tenant rows for this workspace before counting to prevent
        # concurrent removals from both passing the last-tenant guard.
        # NOTE: select_for_update() cannot be combined with .count() in PostgreSQL
        # (FOR UPDATE is not allowed with aggregates), so we evaluate to a list first.
        tenant_ids = list(
            workspace.workspace_tenants.select_for_update().values_list("id", flat=True)
        )
        if len(tenant_ids) <= 1:
            raise ValidationError("Cannot remove the last tenant from a workspace.")
        wt.delete()
        WorkspaceViewSchema.objects.filter(workspace=workspace).update(
            state=SchemaState.PROVISIONING
        )
        rebuild_workspace_view_schema.delay_on_commit(str(workspace.id))


async def touch_workspace_schemas(workspace) -> None:
    """Reset the inactivity TTL for active schemas associated with a workspace.

    For single-tenant workspaces, touches the TenantSchema of the sole tenant.
    For multi-tenant workspaces, touches the WorkspaceViewSchema.
    """
    from asgiref.sync import sync_to_async

    from apps.projects.models import TenantSchema

    tenant_count = await workspace.workspace_tenants.acount()
    if tenant_count == 1:
        tenant = await workspace.tenants.afirst()
        ts = await TenantSchema.objects.filter(
            tenant=tenant,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if ts is not None:
            await sync_to_async(ts.touch)()
    elif tenant_count > 1:
        vs = await WorkspaceViewSchema.objects.filter(
            workspace=workspace,
            state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
        ).afirst()
        if vs is not None:
            await sync_to_async(vs.touch)()
