"""
Django admin configuration for Artifact models.

Provides admin interfaces for managing Artifacts and SharedArtifacts
with filtering, search, and inline editing capabilities.
"""
from django.contrib import admin
from django.utils.html import format_html

from .models import AccessLevel, Artifact, ArtifactType, SharedArtifact


class SharedArtifactInline(admin.TabularInline):
    """Inline admin for SharedArtifact on Artifact detail page."""

    model = SharedArtifact
    extra = 0
    readonly_fields = ("share_token", "view_count", "created_at", "share_url_display")
    fields = (
        "share_token",
        "access_level",
        "expires_at",
        "view_count",
        "created_at",
        "share_url_display",
    )

    def share_url_display(self, obj):
        """Display the share URL as a clickable link."""
        if obj.pk:
            return format_html('<a href="{}" target="_blank">{}</a>', obj.share_url, obj.share_url)
        return "-"

    share_url_display.short_description = "Share URL"


@admin.register(Artifact)
class ArtifactAdmin(admin.ModelAdmin):
    """Admin interface for Artifact model."""

    list_display = (
        "title",
        "artifact_type",
        "project",
        "created_by",
        "version",
        "created_at",
        "code_preview",
    )
    list_filter = (
        "artifact_type",
        "project",
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
    raw_id_fields = ("project", "created_by", "parent_artifact")
    date_hierarchy = "created_at"
    inlines = [SharedArtifactInline]

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
                    "project",
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


@admin.register(SharedArtifact)
class SharedArtifactAdmin(admin.ModelAdmin):
    """Admin interface for SharedArtifact model."""

    list_display = (
        "artifact",
        "access_level",
        "created_by",
        "share_token_short",
        "view_count",
        "expires_at",
        "is_expired_display",
        "created_at",
    )
    list_filter = (
        "access_level",
        "created_at",
        "expires_at",
    )
    search_fields = (
        "share_token",
        "artifact__title",
        "created_by__email",
    )
    readonly_fields = (
        "id",
        "share_token",
        "view_count",
        "created_at",
        "share_url_display",
        "is_expired_display",
    )
    raw_id_fields = ("artifact", "created_by")
    filter_horizontal = ("allowed_users",)
    date_hierarchy = "created_at"

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "id",
                    "artifact",
                    "created_by",
                )
            },
        ),
        (
            "Share Configuration",
            {
                "fields": (
                    "share_token",
                    "share_url_display",
                    "access_level",
                    "allowed_users",
                )
            },
        ),
        (
            "Expiration",
            {
                "fields": (
                    "expires_at",
                    "is_expired_display",
                )
            },
        ),
        (
            "Statistics",
            {
                "fields": (
                    "view_count",
                    "created_at",
                )
            },
        ),
    )

    def share_token_short(self, obj):
        """Display truncated share token."""
        return f"{obj.share_token[:12]}..."

    share_token_short.short_description = "Token"

    def share_url_display(self, obj):
        """Display the share URL as a clickable link."""
        return format_html('<a href="{}" target="_blank">{}</a>', obj.share_url, obj.share_url)

    share_url_display.short_description = "Share URL"

    def is_expired_display(self, obj):
        """Display expiration status with color coding."""
        if obj.is_expired:
            return format_html('<span style="color: red;">Expired</span>')
        return format_html('<span style="color: green;">Active</span>')

    is_expired_display.short_description = "Status"

    actions = ["regenerate_tokens"]

    @admin.action(description="Regenerate share tokens for selected shares")
    def regenerate_tokens(self, request, queryset):
        """Regenerate share tokens for selected SharedArtifact instances."""
        count = 0
        for share in queryset:
            share.share_token = SharedArtifact.generate_token()
            share.save(update_fields=["share_token"])
            count += 1
        self.message_user(request, f"Regenerated tokens for {count} share(s).")
