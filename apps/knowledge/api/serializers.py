"""Serializers for knowledge management API."""

from rest_framework import serializers

from apps.common.utils import creator_display_name
from apps.knowledge.models import KnowledgeEntry


class KnowledgeEntrySerializer(serializers.ModelSerializer):
    type = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeEntry
        fields = [
            "id",
            "type",
            "title",
            "content",
            "tags",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "type", "created_by_name", "created_at", "updated_at"]

    def get_type(self, obj) -> str:
        return "entry"

    def get_created_by_name(self, obj):

        return creator_display_name(obj.created_by)
