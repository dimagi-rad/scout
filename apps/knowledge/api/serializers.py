"""Serializers for knowledge management API."""

from rest_framework import serializers

from apps.knowledge.models import AgentLearning, KnowledgeEntry


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
        from apps.common.utils import creator_display_name

        return creator_display_name(obj.created_by)


class AgentLearningSerializer(serializers.ModelSerializer):
    type = serializers.SerializerMethodField()

    class Meta:
        model = AgentLearning
        fields = [
            "id",
            "type",
            "description",
            "category",
            "applies_to_tables",
            "original_error",
            "original_sql",
            "corrected_sql",
            "confidence_score",
            "times_applied",
            "is_active",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "type",
            "original_error",
            "original_sql",
            "corrected_sql",
            "confidence_score",
            "times_applied",
            "created_at",
        ]

    def get_type(self, obj) -> str:
        return "learning"
