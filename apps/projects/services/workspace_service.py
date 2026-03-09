"""Service functions for workspace tenant management."""

from __future__ import annotations

from django.db import transaction

from apps.projects.models import SchemaState, WorkspaceTenant, WorkspaceViewSchema


def add_workspace_tenant(workspace, tenant) -> WorkspaceTenant:
    """Add a tenant to a workspace and mark the view schema for rebuild.

    Creates the WorkspaceTenant record and marks any existing WorkspaceViewSchema
    as PROVISIONING, then dispatches a rebuild task after the transaction commits.

    Returns the new WorkspaceTenant instance.
    """
    from apps.projects.tasks import rebuild_workspace_view_schema

    with transaction.atomic():
        wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
        WorkspaceViewSchema.objects.filter(workspace=workspace).update(
            state=SchemaState.PROVISIONING
        )
        workspace_id_str = str(workspace.id)
        transaction.on_commit(lambda: rebuild_workspace_view_schema.delay(workspace_id_str))

    return wt


def remove_workspace_tenant(workspace, wt: WorkspaceTenant) -> None:
    """Remove a tenant from a workspace and mark the view schema for rebuild.

    Deletes the WorkspaceTenant record and marks any existing WorkspaceViewSchema
    as PROVISIONING, then dispatches a rebuild task after the transaction commits.
    """
    from apps.projects.tasks import rebuild_workspace_view_schema

    with transaction.atomic():
        wt.delete()
        WorkspaceViewSchema.objects.filter(workspace=workspace).update(
            state=SchemaState.PROVISIONING
        )
        workspace_id_str = str(workspace.id)
        transaction.on_commit(lambda: rebuild_workspace_view_schema.delay(workspace_id_str))
