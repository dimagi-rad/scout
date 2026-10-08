from django.contrib import admin
from django.core.paginator import Paginator
from django.db import connection
from django.utils.functional import cached_property

from apps.common.admin import ReadOnlyModelAdmin

from .models import TelemetryEvent

EXACT_COUNT_BOUND = 10_000


class EstimatedCountPaginator(Paginator):
    """Pages by the planner's row estimate: an exact COUNT(*) scans a year of events."""

    @cached_property
    def count(self) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
                [self.object_list.model._meta.db_table],
            )
            row = cursor.fetchone()
        estimate = row[0] if row else 0
        if estimate >= EXACT_COUNT_BOUND:
            return estimate
        # Before the first ANALYZE the estimate is 0 or -1, and a count that fits on
        # one page makes the changelist skip the paginator and load every row, so
        # small or unknown tables get a count that stops at the bound.
        return self.object_list[:EXACT_COUNT_BOUND].count()


@admin.register(TelemetryEvent)
class TelemetryEventAdmin(ReadOnlyModelAdmin):
    # No list filters or date hierarchy: each runs a DISTINCT scan of a table that
    # holds a year of events on the shared database.
    list_display = ["occurred_at", "kind", "name", "outcome", "duration_ms", "workspace_id"]
    show_full_result_count = False
    paginator = EstimatedCountPaginator
    ordering = ["-occurred_at"]
