"""
Serializers for recipes API.
"""
from rest_framework import serializers

from apps.recipes.models import Recipe, RecipeRun


class RecipeListSerializer(serializers.ModelSerializer):
    """Serializer for recipe list view."""

    variable_count = serializers.SerializerMethodField()
    last_run_at = serializers.SerializerMethodField()

    class Meta:
        model = Recipe
        fields = [
            "id",
            "name",
            "description",
            "is_shared",
            "is_public",
            "share_token",
            "variable_count",
            "last_run_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_variable_count(self, obj):
        return len(obj.variables) if obj.variables else 0

    def get_last_run_at(self, obj):
        last_run = obj.runs.order_by("-created_at").first()
        return last_run.created_at if last_run else None


class RecipeDetailSerializer(serializers.ModelSerializer):
    """Serializer for recipe detail/update."""

    class Meta:
        model = Recipe
        fields = [
            "id",
            "name",
            "description",
            "prompt",
            "variables",
            "is_shared",
            "is_public",
            "share_token",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "share_token", "created_at", "updated_at"]


class RecipeUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating a recipe."""

    class Meta:
        model = Recipe
        fields = ["name", "description", "prompt", "variables", "is_shared", "is_public"]


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
            "is_shared",
            "is_public",
            "share_token",
            "started_at",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields


class RecipeRunUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating recipe run sharing settings."""

    class Meta:
        model = RecipeRun
        fields = ["is_shared", "is_public"]


class PublicRecipeSerializer(serializers.ModelSerializer):
    """Read-only serializer for public access to a recipe."""

    class Meta:
        model = Recipe
        fields = [
            "id",
            "name",
            "description",
            "prompt",
            "variables",
            "created_at",
        ]
        read_only_fields = fields


class PublicRecipeRunSerializer(serializers.ModelSerializer):
    """Read-only serializer for public access to a recipe run."""

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
