"""Serializers for knowledge management API."""

from rest_framework import serializers

from apps.common.utils import creator_display_name
from apps.knowledge.models import KnowledgeEntry

# Responses carry only this much of a reference document, which can be megabytes;
# the detail GET (``full_content`` in the context) returns all of it.
REFERENCE_PREVIEW_CHARS = 500


def _shown_content(obj: KnowledgeEntry, full: bool) -> str:
    # The list view defers ``content`` and annotates the preview as ``listed_content``.
    listed = getattr(obj, "listed_content", None)
    if listed is not None:
        return listed
    if full or obj.kind != KnowledgeEntry.Kind.REFERENCE:
        return obj.content
    return obj.content[:REFERENCE_PREVIEW_CHARS]


class _ContentField(serializers.CharField):
    def get_attribute(self, instance):
        return _shown_content(instance, self.context.get("full_content", False))


class KnowledgeEntrySerializer(serializers.ModelSerializer):
    type = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    content = _ContentField()
    size = serializers.SerializerMethodField()
    content_truncated = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeEntry
        fields = [
            "id",
            "type",
            "kind",
            "title",
            "description",
            "source_format",
            "content",
            "content_truncated",
            "size",
            "tags",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "type",
            "size",
            "content_truncated",
            "created_by_name",
            "created_at",
            "updated_at",
        ]

    def get_type(self, obj) -> str:
        return "entry"

    def get_created_by_name(self, obj):
        return creator_display_name(obj.created_by)

    def get_size(self, obj) -> int:
        size = getattr(obj, "content_size", None)
        return size if size is not None else len(obj.content)

    def get_content_truncated(self, obj) -> bool:
        shown = _shown_content(obj, self.context.get("full_content", False))
        return len(shown) < self.get_size(obj)
