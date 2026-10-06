"""Admin configuration for Knowledge models."""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    AgentLearning,
    KnowledgeEntry,
    TableKnowledge,
)


@admin.register(TableKnowledge)
class TableKnowledgeAdmin(admin.ModelAdmin):
    list_display = ["table_name", "workspace", "owner", "refresh_frequency", "updated_at"]
    list_filter = ["workspace", "updated_at"]
    search_fields = ["table_name", "description", "owner"]
    autocomplete_fields = ["updated_by"]

    fieldsets = (
        (None, {"fields": ("workspace", "table_name")}),
        ("Description", {"fields": ("description", "use_cases")}),
        (
            "Data Quality",
            {"fields": ("data_quality_notes", "owner", "refresh_frequency")},
        ),
        ("Relationships", {"fields": ("related_tables", "column_notes")}),
        (
            "Metadata",
            {
                "fields": ("updated_by", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )
    readonly_fields = ["created_at", "updated_at"]

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(KnowledgeEntry)
class KnowledgeEntryAdmin(admin.ModelAdmin):
    list_display = ["title", "workspace", "tags_display", "updated_at"]
    list_filter = ["workspace", "updated_at"]
    search_fields = ["title", "content"]
    autocomplete_fields = ["created_by"]
    readonly_fields = ["created_at", "updated_at"]

    @admin.display(description="Tags")
    def tags_display(self, obj):
        return ", ".join(obj.tags) if obj.tags else "-"

    def save_model(self, request, obj, form, change):
        if not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


class ConfidenceRangeFilter(admin.SimpleListFilter):
    title = "confidence range"
    parameter_name = "confidence_range"

    def lookups(self, request, model_admin):
        return [
            ("high", "High (0.8 - 1.0)"),
            ("medium", "Medium (0.5 - 0.8)"),
            ("low", "Low (0.0 - 0.5)"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "high":
            return queryset.filter(confidence_score__gte=0.8)
        elif self.value() == "medium":
            return queryset.filter(confidence_score__gte=0.5, confidence_score__lt=0.8)
        elif self.value() == "low":
            return queryset.filter(confidence_score__lt=0.5)
        return queryset


@admin.register(AgentLearning)
class AgentLearningAdmin(admin.ModelAdmin):
    list_display = [
        "description_short",
        "workspace",
        "category",
        "confidence_badge",
        "times_applied",
        "is_active",
        "created_at",
    ]
    list_filter = ["workspace", "category", "is_active", ConfidenceRangeFilter]
    search_fields = ["description", "original_error"]
    actions = None

    fieldsets = (
        (None, {"fields": ("workspace", "description", "category")}),
        ("Scope", {"fields": ("applies_to_tables",)}),
        (
            "Evidence",
            {
                "fields": ("original_error",),
                "classes": ("collapse",),
            },
        ),
        (
            "Lifecycle",
            {"fields": ("confidence_score", "times_applied", "is_active")},
        ),
        (
            "Source",
            {
                "fields": (
                    "discovered_in_conversation",
                    "discovered_by_user",
                    "created_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    # Workspace memory changes only through the Memory page and the agent tool,
    # which enforce author-or-manager edits, keep every memory within the prompt
    # budget and record WorkspaceMemoryEvents. The admin is for inspection.
    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Description")
    def description_short(self, obj):
        return obj.description[:80] + "..." if len(obj.description) > 80 else obj.description

    @admin.display(description="Confidence")
    def confidence_badge(self, obj):
        score = obj.confidence_score
        if score >= 0.8:
            color = "green"
        elif score >= 0.5:
            color = "orange"
        else:
            color = "red"
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            f"{score:.0%}",
        )
