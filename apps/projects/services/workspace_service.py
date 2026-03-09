"""Service functions for workspace tenant management."""

from __future__ import annotations

from django.core.exceptions import ValidationError
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

    Raises ValidationError if wt is the last tenant in the workspace.
    """
    from apps.projects.tasks import rebuild_workspace_view_schema

    with transaction.atomic():
        # Lock all tenant rows for this workspace before counting to prevent
        # concurrent removals from both passing the last-tenant guard.
        count = workspace.workspace_tenants.select_for_update().count()
        if count <= 1:
            raise ValidationError("Cannot remove the last tenant from a workspace.")
        wt.delete()
        WorkspaceViewSchema.objects.filter(workspace=workspace).update(
            state=SchemaState.PROVISIONING
        )
        workspace_id_str = str(workspace.id)
        transaction.on_commit(lambda: rebuild_workspace_view_schema.delay(workspace_id_str))
