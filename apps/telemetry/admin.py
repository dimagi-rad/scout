from django.contrib import admin

from apps.common.admin import ReadOnlyModelAdmin

from .models import TelemetryEvent


@admin.register(TelemetryEvent)
class TelemetryEventAdmin(ReadOnlyModelAdmin):
    list_display = ["occurred_at", "kind", "name", "outcome", "duration_ms", "workspace_id"]
    list_filter = ["kind", "outcome"]
    date_hierarchy = "occurred_at"
