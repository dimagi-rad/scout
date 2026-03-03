"""
Admin configuration for projects app.
"""

from django.contrib import admin

from .models import MaterializationRun, TenantSchema, TenantWorkspace


@admin.register(TenantWorkspace)
class TenantWorkspaceAdmin(admin.ModelAdmin):
    list_display = ["tenant_name", "tenant_id", "created_at", "updated_at"]
    search_fields = ["tenant_name", "tenant_id"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(TenantSchema)
class TenantSchemaAdmin(admin.ModelAdmin):
    list_display = ["schema_name", "state", "tenant_membership", "created_at"]
    list_filter = ["state"]
    readonly_fields = ["id", "created_at"]


@admin.register(MaterializationRun)
class MaterializationRunAdmin(admin.ModelAdmin):
    list_display = ["pipeline", "state", "tenant_schema", "started_at", "completed_at"]
    list_filter = ["state", "pipeline"]
    readonly_fields = ["id", "started_at"]
