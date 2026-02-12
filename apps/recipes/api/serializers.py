"""
Serializers for recipes API.
"""
from rest_framework import serializers

from apps.recipes.models import Recipe, RecipeRun, RecipeStep


class RecipeStepSerializer(serializers.ModelSerializer):
    """Serializer for recipe steps."""

    class Meta:
        model = RecipeStep
        fields = ["id", "order", "prompt_template"]
        read_only_fields = ["id"]


class RecipeListSerializer(serializers.ModelSerializer):
    """Serializer for recipe list view."""

    step_count = serializers.SerializerMethodField()
    variable_count = serializers.SerializerMethodField()
    last_run_at = serializers.SerializerMethodField()

    class Meta:
        model = Recipe
        fields = [
            "id",
            "name",
            "description",
            "step_count",
            "variable_count",
            "last_run_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_step_count(self, obj):
        return obj.steps.count()

    def get_variable_count(self, obj):
        return len(obj.variables) if obj.variables else 0

    def get_last_run_at(self, obj):
        last_run = obj.runs.order_by("-created_at").first()
        return last_run.created_at if last_run else None


class RecipeDetailSerializer(serializers.ModelSerializer):
    """Serializer for recipe detail/update."""

    steps = RecipeStepSerializer(many=True, read_only=True)

    class Meta:
        model = Recipe
        fields = [
            "id",
            "name",
            "description",
            "variables",
            "steps",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class RecipeUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating recipe with steps."""

    steps = RecipeStepSerializer(many=True)

    class Meta:
        model = Recipe
        fields = ["name", "description", "variables", "steps"]

    def update(self, instance, validated_data):
        steps_data = validated_data.pop("steps", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if steps_data is not None:
            # Clear existing steps and recreate
            instance.steps.all().delete()
            for i, step_data in enumerate(steps_data):
                RecipeStep.objects.create(
                    recipe=instance,
                    order=step_data.get("order", i),
                    prompt_template=step_data["prompt_template"],
                )

        return instance


class RunRecipeSerializer(serializers.Serializer):
    """Serializer for running a recipe."""

    variables = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
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
