from django.contrib import admin

from .models import (
    SemanticCanvas,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)


class SemanticFieldInline(admin.TabularInline):
    model = SemanticField
    extra = 0
    fields = (
        "name",
        "label",
        "field_type",
        "data_type",
        "expression",
        "measure_type",
        "is_visible",
    )


@admin.register(SemanticModel)
class SemanticModelAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "version", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "workspace__name")
    raw_id_fields = ("workspace",)


@admin.register(SemanticDataset)
class SemanticDatasetAdmin(admin.ModelAdmin):
    list_display = ("name", "label", "workspace", "table_name", "is_visible", "updated_at")
    list_filter = ("is_visible", "workspace")
    search_fields = ("name", "label", "description", "table_name")
    raw_id_fields = ("semantic_model", "workspace")
    inlines = (SemanticFieldInline,)


@admin.register(SemanticField)
class SemanticFieldAdmin(admin.ModelAdmin):
    list_display = ("member_name", "field_type", "data_type", "measure_type", "is_visible")
    list_filter = ("field_type", "measure_type", "is_visible")
    search_fields = ("name", "label", "dataset__name")
    raw_id_fields = ("dataset",)


@admin.register(SemanticRelationship)
class SemanticRelationshipAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "from_dataset", "to_dataset", "relationship_type")
    list_filter = ("relationship_type", "workspace")
    search_fields = ("name", "join_expression")
    raw_id_fields = ("workspace", "from_dataset", "to_dataset")


@admin.register(SemanticCanvas)
class SemanticCanvasAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "status", "created_by", "updated_at")
    list_filter = ("status", "workspace")
    raw_id_fields = ("workspace", "semantic_model", "created_by")
