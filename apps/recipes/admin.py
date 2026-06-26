"""
Admin configuration for Recipe models.

The runner executes a single ``Recipe.prompt`` (recipes/services/runner.py),
not the vestigial ``RecipeStep`` rows. The admin now surfaces ``prompt`` and
drops the RecipeStep inline / ModelAdmin so operators stop creating step rows
nothing reads (arch #260, 11#5).
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import Recipe, RecipeRun


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    """Admin interface for Recipe model."""

    list_display = [
        "name",
        "workspace",
        "prompt_preview",
        "variable_count",
        "is_shared",
        "created_by",
        "updated_at",
    ]
    list_filter = ["is_shared", "created_at", "workspace"]
    search_fields = ["name", "description", "prompt"]
    readonly_fields = ["id", "share_token", "created_at", "updated_at"]
    autocomplete_fields = ["created_by"]

    fieldsets = (
        (None, {"fields": ("id", "name", "description", "workspace")}),
        (
            "Prompt",
            {
                "fields": ("prompt",),
                "description": "The prompt template the runner executes. "
                "Supports {{variable}} placeholders and markdown.",
            },
        ),
        (
            "Variables",
            {
                "fields": ("variables",),
                "description": "Define variables as a JSON list. Each variable should have: "
                "name, type (string/number/date/boolean/select), label, and optionally "
                "default and options (for select type).",
            },
        ),
        (
            "Sharing",
            {
                "fields": ("is_shared", "is_public", "share_token"),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("created_by", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Prompt")
    def prompt_preview(self, obj):
        preview = (obj.prompt or "")[:80]
        if obj.prompt and len(obj.prompt) > 80:
            preview += "..."
        return preview or "-"

    @admin.display(description="Variables")
    def variable_count(self, obj):
        return len(obj.variables) if obj.variables else 0


@admin.register(RecipeRun)
class RecipeRunAdmin(admin.ModelAdmin):
    """Admin interface for RecipeRun model."""

    list_display = [
        "id",
        "recipe",
        "status_badge",
        "step_progress",
        "run_by",
        "duration_display",
        "created_at",
    ]
    list_filter = ["status", "is_shared", "is_public", "created_at", "recipe__workspace"]
    search_fields = ["recipe__name", "run_by__email"]
    readonly_fields = [
        "id",
        "recipe",
        "status",
        "variable_values",
        "step_results",
        "share_token",
        "started_at",
        "completed_at",
        "run_by",
        "created_at",
    ]
    autocomplete_fields = ["recipe", "run_by"]

    fieldsets = (
        (None, {"fields": ("id", "recipe", "status")}),
        (
            "Execution",
            {
                "fields": ("variable_values", "step_results"),
            },
        ),
        (
            "Sharing",
            {
                "fields": ("is_shared", "is_public", "share_token"),
            },
        ),
        (
            "Timing",
            {
                "fields": ("started_at", "completed_at"),
            },
        ),
        (
            "Metadata",
            {
                "fields": ("run_by", "created_at"),
            },
        ),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        """Display status with color coding."""
        colors = {
            "pending": "gray",
            "running": "blue",
            "completed": "green",
            "failed": "red",
        }
        color = colors.get(obj.status, "gray")
        return format_html(
            '<span style="color: {};">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Progress")
    def step_progress(self, obj):
        """Show how many step results were recorded for this run."""
        return f"{len(obj.step_results)} step(s)"

    @admin.display(description="Duration")
    def duration_display(self, obj):
        """Display duration in a readable format."""
        duration = obj.duration_seconds
        if duration is None:
            return "-"
        if duration < 60:
            return f"{duration:.1f}s"
        minutes = int(duration // 60)
        seconds = duration % 60
        return f"{minutes}m {seconds:.1f}s"
