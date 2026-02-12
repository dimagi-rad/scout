"""
API views for recipe management.

Provides endpoints for CRUD operations on recipes, running recipes,
and viewing run history.
"""
import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.projects.api.permissions import ProjectPermissionMixin
from apps.recipes.models import Recipe, RecipeRun
from apps.recipes.services.runner import RecipeRunner, RecipeRunnerError, VariableValidationError

from .serializers import (
    PublicRecipeRunSerializer,
    RecipeDetailSerializer,
    RecipeListSerializer,
    RecipeRunSerializer,
    RecipeRunUpdateSerializer,
    RecipeUpdateSerializer,
    RunRecipeSerializer,
)

logger = logging.getLogger(__name__)


class RecipePermissionMixin(ProjectPermissionMixin):
    """Extended permission mixin for recipe operations."""

    def get_recipe(self, project, recipe_id):
        """Retrieve a recipe by ID within a project."""
        return get_object_or_404(
            Recipe,
            pk=recipe_id,
            project=project,
        )


class RecipeListView(RecipePermissionMixin, APIView):
    """List all recipes for a project."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        recipes = Recipe.objects.filter(project=project).prefetch_related("runs")
        serializer = RecipeListSerializer(recipes, many=True)
        return Response(serializer.data)


class RecipeDetailView(RecipePermissionMixin, APIView):
    """Retrieve, update, or delete a recipe."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, recipe_id):
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        serializer = RecipeDetailSerializer(recipe)
        return Response(serializer.data)

    def put(self, request, project_id, recipe_id):
        project = self.get_project(project_id)

        can_edit, error_response = self.check_edit_permission(request, project)
        if not can_edit:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        serializer = RecipeUpdateSerializer(
            recipe,
            data=request.data,
            partial=True,
        )

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()

        response_serializer = RecipeDetailSerializer(recipe)
        return Response(response_serializer.data)

    def delete(self, request, project_id, recipe_id):
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        recipe.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RecipeRunView(RecipePermissionMixin, APIView):
    """Run a recipe with provided variable values."""

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id, recipe_id):
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        recipe = self.get_recipe(project, recipe_id)

        serializer = RunRecipeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        variables = serializer.validated_data.get("variable_values", {})

        try:
            runner = RecipeRunner(
                recipe=recipe,
                variable_values=variables,
                user=request.user,
            )
            run = runner.execute()
        except VariableValidationError as e:
            return Response(
                {"error": "Variable validation failed", "details": e.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RecipeRunnerError as e:
            logger.exception("Recipe execution error for %s: %s", recipe.name, e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        serializer = RecipeRunSerializer(run)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class RecipeRunHistoryView(RecipePermissionMixin, APIView):
    """List run history for a recipe."""

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, recipe_id):
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        runs = RecipeRun.objects.filter(recipe=recipe).order_by("-created_at")

        serializer = RecipeRunSerializer(runs, many=True)
        return Response(serializer.data)


class RecipeRunUpdateView(RecipePermissionMixin, APIView):
    """Update sharing settings on a recipe run."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, project_id, recipe_id, run_id):
        project = self.get_project(project_id)

        can_edit, error_response = self.check_edit_permission(request, project)
        if not can_edit:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        run = get_object_or_404(RecipeRun, pk=run_id, recipe=recipe)

        serializer = RecipeRunUpdateSerializer(run, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()

        response_serializer = RecipeRunSerializer(run)
        return Response(response_serializer.data)


class PublicRecipeRunView(APIView):
    """Public access to a shared recipe run."""

    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]

    def get(self, request, share_token):
        run = get_object_or_404(
            RecipeRun,
            share_token=share_token,
            is_public=True,
        )
        serializer = PublicRecipeRunSerializer(run)
        return Response(serializer.data)
