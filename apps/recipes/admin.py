"""
Admin configuration for Recipe models.
"""
from django.contrib import admin
from django.utils.html import format_html

from .models import Recipe, RecipeRun, RecipeStep


class RecipeStepInline(admin.TabularInline):
    """Inline admin for recipe steps."""

    model = RecipeStep
    extra = 1
    ordering = ["order"]
    fields = ["order", "prompt_template", "expected_tool", "description"]


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    """Admin interface for Recipe model."""

    list_display = [
        "name",
        "project",
        "step_count",
        "variable_count",
        "is_shared",
        "created_by",
        "updated_at",
    ]
    list_filter = ["is_shared", "created_at", "project"]
    search_fields = ["name", "description"]
    readonly_fields = ["id", "share_token", "created_at", "updated_at"]
    autocomplete_fields = ["project", "created_by"]
    inlines = [RecipeStepInline]

    fieldsets = (
        (None, {"fields": ("id", "name", "description", "project")}),
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

    @admin.display(description="Steps")
    def step_count(self, obj):
        return obj.steps.count()

    @admin.display(description="Variables")
    def variable_count(self, obj):
        return len(obj.variables) if obj.variables else 0


@admin.register(RecipeStep)
class RecipeStepAdmin(admin.ModelAdmin):
    """Admin interface for RecipeStep model."""

    list_display = ["recipe", "order", "prompt_preview", "expected_tool"]
    list_filter = ["recipe__project", "expected_tool"]
    search_fields = ["prompt_template", "description", "recipe__name"]
    autocomplete_fields = ["recipe"]
    ordering = ["recipe", "order"]

    @admin.display(description="Prompt")
    def prompt_preview(self, obj):
        """Show a truncated preview of the prompt template."""
        preview = obj.prompt_template[:100]
        if len(obj.prompt_template) > 100:
            preview += "..."
        return preview


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
    list_filter = ["status", "is_shared", "is_public", "created_at", "recipe__project"]
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
        """Show step progress as completed/total."""
        total_steps = obj.recipe.steps.count()
        completed_steps = len(obj.step_results)
        return f"{completed_steps}/{total_steps}"

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
