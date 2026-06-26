"""
Admin configuration for the workspaces app.

State-machine rows (TenantSchema, MaterializationRun) are intentionally
hardened: their state/identity fields are readonly so an admin edit can never
re-arm expire_inactive_schemas -> teardown_schema -> DROP SCHEMA CASCADE,
clobber a live transition, or desync the physical schema from its Django row
(arch #260, 11#3). Operator models are registered read-only for inspection
(11#5).
"""

from django.contrib import admin

from apps.common.admin import ReadOnlyModelAdmin

from .models import (
    MaterializationRun,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceTenant,
    WorkspaceViewSchema,
)


@admin.register(TenantSchema)
class TenantSchemaAdmin(admin.ModelAdmin):
    list_display = ["schema_name", "state", "tenant", "created_at"]
    list_filter = ["state"]
    # state/schema_name/last_accessed_at are part of the schema lifecycle
    # state machine — editing them via admin is a DROP-SCHEMA / desync footgun.
    readonly_fields = ["id", "tenant", "schema_name", "state", "last_accessed_at", "created_at"]

    def has_delete_permission(self, request, obj=None):
        # A default cascade delete would orphan the physical schema + _ro role.
        return False


@admin.register(MaterializationRun)
class MaterializationRunAdmin(admin.ModelAdmin):
    list_display = ["pipeline", "state", "tenant_schema", "started_at", "completed_at"]
    list_filter = ["state", "pipeline"]
    readonly_fields = [
        "id",
        "tenant_schema",
        "pipeline",
        "state",
        "result",
        "progress",
        "procrastinate_job_id",
        "started_at",
        "completed_at",
    ]


@admin.register(Workspace)
class WorkspaceAdmin(ReadOnlyModelAdmin):
    list_display = ["name", "is_auto_created", "created_by", "created_at"]
    list_filter = ["is_auto_created", "created_at"]
    search_fields = ["name", "id"]


@admin.register(WorkspaceTenant)
class WorkspaceTenantAdmin(ReadOnlyModelAdmin):
    list_display = ["workspace", "tenant"]
    search_fields = ["workspace__name", "tenant__canonical_name", "tenant__external_id"]


@admin.register(WorkspaceMembership)
class WorkspaceMembershipAdmin(ReadOnlyModelAdmin):
    list_display = ["workspace", "user", "role", "invited_by", "created_at"]
    list_filter = ["role", "created_at"]
    search_fields = ["workspace__name", "user__email"]


@admin.register(WorkspaceViewSchema)
class WorkspaceViewSchemaAdmin(ReadOnlyModelAdmin):
    list_display = ["schema_name", "state", "workspace", "last_accessed_at", "created_at"]
    list_filter = ["state"]
    search_fields = ["schema_name", "workspace__name"]
