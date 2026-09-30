"""
Serializers for recipes API.
"""

from rest_framework import serializers

from apps.common.utils import creator_display_name
from apps.recipes.models import Recipe, RecipeRun


class RecipeListSerializer(serializers.ModelSerializer):
    """Serializer for recipe list view."""

    variable_count = serializers.SerializerMethodField()
    last_run_at = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Recipe
        fields = [
            "id",
            "name",
            "description",
            "variable_count",
            "last_run_at",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_variable_count(self, obj):
        return len(obj.variables) if obj.variables else 0

    def get_last_run_at(self, obj):
        last_run = obj.runs.order_by("-created_at").first()
        return last_run.created_at if last_run else None

    def get_created_by_name(self, obj):

        return creator_display_name(obj.created_by)


class RecipeDetailSerializer(serializers.ModelSerializer):
    """Serializer for recipe detail/update."""

    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Recipe
        fields = [
            "id",
            "name",
            "description",
            "prompt",
            "variables",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_by_name", "created_at", "updated_at"]

    def get_created_by_name(self, obj):

        return creator_display_name(obj.created_by)


class RecipeUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating a recipe."""

    class Meta:
        model = Recipe
        fields = ["name", "description", "prompt", "variables"]


class RunRecipeSerializer(serializers.Serializer):
    """Serializer for running a recipe."""

    variable_values = serializers.DictField(
        required=False,
        default=dict,
    )


class RecipeRunSerializer(serializers.ModelSerializer):
    """Serializer for recipe run history."""

    class Meta:
        model = RecipeRun
        fields = [
            "id",
            "status",
            "variable_values",
            "step_results",
            "started_at",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields
