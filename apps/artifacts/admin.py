"""
Django admin configuration for Artifact models.

Provides admin interfaces for managing Artifacts
with filtering, search, and inline editing capabilities.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import Artifact


@admin.register(Artifact)
class ArtifactAdmin(admin.ModelAdmin):
    """Admin interface for Artifact model."""

    list_display = (
        "title",
        "artifact_type",
        "workspace",
        "created_by",
        "version",
        "created_at",
        "code_preview",
    )
    list_filter = (
        "artifact_type",
        "workspace",
        "created_at",
    )
    search_fields = (
        "title",
        "description",
        "code",
        "conversation_id",
    )
    readonly_fields = (
        "id",
        "content_hash_display",
        "created_at",
        "updated_at",
        "version_history_display",
    )
    raw_id_fields = ("workspace", "created_by", "parent_artifact")
    date_hierarchy = "created_at"

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "title",
                    "description",
                    "artifact_type",
                )
            },
        ),
        (
            "Content",
            {
                "fields": (
                    "code",
                    "data",
                    "content_hash_display",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Relationships",
            {
                "fields": (
                    "workspace",
                    "created_by",
                    "conversation_id",
                )
            },
        ),
        (
            "Versioning",
            {
                "fields": (
                    "version",
                    "parent_artifact",
                    "version_history_display",
                )
            },
        ),
        (
            "Source Data",
            {
                "fields": ("source_queries",),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    def code_preview(self, obj):
        """Display truncated code preview."""
        if obj.code:
            preview = obj.code[:100]
            if len(obj.code) > 100:
                preview += "..."
            return preview
        return "-"

    code_preview.short_description = "Code Preview"

    def content_hash_display(self, obj):
        """Display the content hash."""
        return obj.content_hash

    content_hash_display.short_description = "Content Hash (SHA-256)"

    def version_history_display(self, obj):
        """Display version history as a list of links."""
        history = obj.get_version_history()
        if len(history) <= 1:
            return "No previous versions"

        links = []
        for artifact in history:
            if artifact.pk == obj.pk:
                links.append(f"<strong>v{artifact.version} (current)</strong>")
            else:
                url = f"/admin/artifacts/artifact/{artifact.pk}/change/"
                links.append(f'<a href="{url}">v{artifact.version}</a>')

        return format_html(" -> ".join(links))

    version_history_display.short_description = "Version History"
