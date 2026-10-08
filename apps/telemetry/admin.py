from django.contrib import admin

from apps.common.admin import ReadOnlyModelAdmin

from .models import TelemetryEvent


@admin.register(TelemetryEvent)
class TelemetryEventAdmin(ReadOnlyModelAdmin):
    # No list filters or date hierarchy: each runs a DISTINCT scan of a table that
    # holds a year of events on the shared database.
    list_display = ["occurred_at", "kind", "name", "outcome", "duration_ms", "workspace_id"]
    show_full_result_count = False
    ordering = ["-occurred_at"]
