from django.contrib import admin

from .models import TransformationAsset, TransformationAssetRun, TransformationRun


class TransformationAssetRunInline(admin.TabularInline):
    model = TransformationAssetRun
    readonly_fields = ("asset", "status", "duration_ms", "started_at", "completed_at")
    extra = 0


@admin.register(TransformationAsset)
class TransformationAssetAdmin(admin.ModelAdmin):
    list_display = ("name", "scope", "tenant", "workspace", "replaces", "updated_at")
    list_filter = ("scope",)
    search_fields = ("name", "description")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(TransformationRun)
class TransformationRunAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "workspace", "status", "started_at", "completed_at")
    list_filter = ("status",)
    inlines = [TransformationAssetRunInline]
    readonly_fields = ("id", "started_at")


@admin.register(TransformationAssetRun)
class TransformationAssetRunAdmin(admin.ModelAdmin):
    list_display = ("asset", "run", "status", "duration_ms", "started_at")
    list_filter = ("status",)
    readonly_fields = ("id", "started_at")
