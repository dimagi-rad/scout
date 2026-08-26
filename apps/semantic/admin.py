from django.contrib import admin

from .models import (
    CubeSchema,
    CustomDataset,
    SemanticCanvas,
    SemanticCanvasChange,
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
    list_display = (
        "name",
        "label",
        "workspace",
        "source_kind",
        "table_name",
        "is_visible",
        "updated_at",
    )
    list_filter = ("source_kind", "is_visible", "workspace")
    search_fields = ("name", "label", "description", "table_name")
    raw_id_fields = ("semantic_model", "workspace", "custom_dataset")
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


@admin.register(CustomDataset)
class CustomDatasetAdmin(admin.ModelAdmin):
    list_display = ("name", "label", "workspace", "status", "is_visible", "updated_at")
    list_filter = ("status", "is_visible", "workspace")
    search_fields = ("name", "label", "description", "definition_sql")
    raw_id_fields = ("workspace", "created_by")
    readonly_fields = ("diagnostics",)


@admin.register(CubeSchema)
class CubeSchemaAdmin(admin.ModelAdmin):
    list_display = ("filename", "workspace", "semantic_model", "status", "content_hash", "updated_at")
    list_filter = ("status", "workspace")
    search_fields = ("filename", "content_hash", "content")
    raw_id_fields = ("workspace", "semantic_model")
    readonly_fields = ("content_hash", "diagnostics", "created_at", "updated_at")


@admin.register(SemanticCanvas)
class SemanticCanvasAdmin(admin.ModelAdmin):
    list_display = ("id", "thread", "workspace", "status", "committed_at", "updated_at")
    list_filter = ("status", "workspace")
    raw_id_fields = ("thread", "workspace", "semantic_model", "created_by")


@admin.register(SemanticCanvasChange)
class SemanticCanvasChangeAdmin(admin.ModelAdmin):
    list_display = ("canvas", "object_type", "object_uuid", "change_type", "updated_at")
    list_filter = ("object_type", "change_type")
    raw_id_fields = ("canvas",)
    readonly_fields = ("created_at", "updated_at")
