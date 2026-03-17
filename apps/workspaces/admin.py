"""
Admin configuration for projects app.
"""

from django.contrib import admin

from .models import MaterializationRun, TenantSchema


@admin.register(TenantSchema)
class TenantSchemaAdmin(admin.ModelAdmin):
    list_display = ["schema_name", "state", "tenant", "created_at"]
    list_filter = ["state"]
    readonly_fields = ["id", "created_at"]


@admin.register(MaterializationRun)
class MaterializationRunAdmin(admin.ModelAdmin):
    list_display = ["pipeline", "state", "tenant_schema", "started_at", "completed_at"]
    list_filter = ["state", "pipeline"]
    readonly_fields = ["id", "started_at"]
