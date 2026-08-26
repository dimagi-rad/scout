"""Admin configuration for chat models (arch #260, 11#5).

Thread and ThreadJob are registered read-only so operators can inspect the rows
that every recent "stuck Preparing…" / zombie-job incident required examining,
without being able to mutate the job state machine through the admin.
"""

from django.contrib import admin

from apps.common.admin import ReadOnlyModelAdmin

from .models import Thread, ThreadArtifact, ThreadJob


@admin.register(Thread)
class ThreadAdmin(ReadOnlyModelAdmin):
    list_display = ["title", "workspace", "user", "is_shared", "updated_at"]
    list_filter = ["is_shared", "updated_at"]
    search_fields = ["title", "user__email", "workspace__name"]


@admin.register(ThreadJob)
class ThreadJobAdmin(ReadOnlyModelAdmin):
    list_display = [
        "id",
        "thread",
        "job_type",
        "state",
        "procrastinate_job_id",
        "created_at",
        "completed_at",
    ]
    list_filter = ["job_type", "state", "created_at"]
    search_fields = ["thread__title", "tool_call_id"]


@admin.register(ThreadArtifact)
class ThreadArtifactAdmin(ReadOnlyModelAdmin):
    list_display = ["thread", "artifact", "source", "last_seen_at"]
    list_filter = ["source", "last_seen_at"]
    search_fields = ["thread__title", "artifact__title", "message_id", "tool_call_id"]
    raw_id_fields = ["thread", "artifact", "workspace"]
