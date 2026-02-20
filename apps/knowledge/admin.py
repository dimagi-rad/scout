"""Admin configuration for Knowledge models."""

from django.contrib import admin

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
    search_fields = ["description", "original_error", "original_sql", "corrected_sql"]
    actions = [
        "approve_learnings",
        "reject_learnings",
        "increase_confidence",
        "decrease_confidence",
    ]

    fieldsets = (
        (None, {"fields": ("workspace", "description", "category")}),
        ("Scope", {"fields": ("applies_to_tables",)}),
        (
            "Evidence",
            {
                "fields": ("original_error", "original_sql", "corrected_sql"),
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
    readonly_fields = ["times_applied", "created_at"]

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
        return f'<span style="color: {color}; font-weight: bold;">{score:.0%}</span>'

    confidence_badge.allow_tags = True

    @admin.action(description="Approve learnings (activate + increase confidence)")
    def approve_learnings(self, request, queryset):
        count = 0
        for learning in queryset:
            learning.is_active = True
            learning.confidence_score = min(1.0, learning.confidence_score + 0.1)
            learning.save(update_fields=["is_active", "confidence_score"])
            count += 1
        self.message_user(request, f"Approved {count} learnings")

    @admin.action(description="Reject learnings (deactivate)")
    def reject_learnings(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"Rejected {count} learnings")

    @admin.action(description="Increase confidence (+10%)")
    def increase_confidence(self, request, queryset):
        count = 0
        for learning in queryset:
            learning.increase_confidence(0.1)
            count += 1
        self.message_user(request, f"Increased confidence for {count} learnings")

    @admin.action(description="Decrease confidence (-10%)")
    def decrease_confidence(self, request, queryset):
        count = 0
        for learning in queryset:
            learning.decrease_confidence(0.1)
            count += 1
        self.message_user(request, f"Decreased confidence for {count} learnings")
