"""Serializers for knowledge management API."""

from rest_framework import serializers

from apps.knowledge.models import AgentLearning, KnowledgeEntry


class KnowledgeEntrySerializer(serializers.ModelSerializer):
    type = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeEntry
        fields = [
            "id",
            "type",
            "title",
            "content",
            "tags",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "type", "created_at", "updated_at"]

    def get_type(self, obj) -> str:
        return "entry"


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
