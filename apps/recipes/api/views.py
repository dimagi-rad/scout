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
    PublicRecipeSerializer,
    RecipeDetailSerializer,
    RecipeListSerializer,
    RecipeRunSerializer,
    RecipeRunUpdateSerializer,
    RecipeUpdateSerializer,
    RunRecipeSerializer,
)

logger = logging.getLogger(__name__)


class RecipePermissionMixin(ProjectPermissionMixin):
    """
    Extended permission mixin for recipe operations.

    Adds recipe-specific helper methods on top of base project permissions.
    """

    def get_recipe(self, project, recipe_id):
        """Retrieve a recipe by ID within a project."""
        return get_object_or_404(
            Recipe.objects.prefetch_related("steps"),
            pk=recipe_id,
            project=project,
        )


class RecipeListView(RecipePermissionMixin, APIView):
    """
    List all recipes for a project.

    GET /api/projects/{project_id}/recipes/
        Returns list of recipes for the project.
        Requires project membership.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        """List all recipes for a project."""
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        recipes = Recipe.objects.filter(project=project).prefetch_related(
            "steps", "runs"
        )
        serializer = RecipeListSerializer(recipes, many=True)
        return Response(serializer.data)


class RecipeDetailView(RecipePermissionMixin, APIView):
    """
    Retrieve, update, or delete a recipe.

    GET /api/projects/{project_id}/recipes/{recipe_id}/
        Returns recipe details with steps. Requires project membership.

    PUT /api/projects/{project_id}/recipes/{recipe_id}/
        Updates recipe and its steps. Requires project membership.

    DELETE /api/projects/{project_id}/recipes/{recipe_id}/
        Deletes recipe. Requires admin role.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, recipe_id):
        """Retrieve a recipe by ID."""
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        serializer = RecipeDetailSerializer(recipe)
        return Response(serializer.data)

    def put(self, request, project_id, recipe_id):
        """Update a recipe."""
        project = self.get_project(project_id)

        # Require analyst or admin permission to edit recipes
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

        # Return the updated recipe with full details
        response_serializer = RecipeDetailSerializer(recipe)
        return Response(response_serializer.data)

    def delete(self, request, project_id, recipe_id):
        """Delete a recipe."""
        project = self.get_project(project_id)

        is_admin, error_response = self.check_admin_permission(request, project)
        if not is_admin:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        recipe.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RecipeRunView(RecipePermissionMixin, APIView):
    """
    Run a recipe with provided variable values.

    POST /api/projects/{project_id}/recipes/{recipe_id}/run/
        Executes the recipe with provided variables via RecipeRunner.
        Returns the completed (or failed) RecipeRun record.
        Requires project membership.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, project_id, recipe_id):
        """Run a recipe."""
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
    """
    List run history for a recipe.

    GET /api/projects/{project_id}/recipes/{recipe_id}/runs/
        Returns list of recipe runs ordered by creation date (newest first).
        Requires project membership.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, recipe_id):
        """List run history for a recipe."""
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        recipe = self.get_recipe(project, recipe_id)
        runs = RecipeRun.objects.filter(recipe=recipe).order_by("-created_at")

        serializer = RecipeRunSerializer(runs, many=True)
        return Response(serializer.data)


class RecipeRunUpdateView(RecipePermissionMixin, APIView):
    """
    Update sharing settings on a recipe run.

    PATCH /api/projects/{project_id}/recipes/{recipe_id}/runs/{run_id}/
        Toggle is_shared / is_public on a run.
        Requires edit permission (analyst/admin).
    """

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


class PublicRecipeView(APIView):
    """
    Public access to a shared recipe.

    GET /api/recipes/shared/{share_token}/
        No authentication required.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]

    def get(self, request, share_token):
        recipe = get_object_or_404(
            Recipe.objects.prefetch_related("steps"),
            share_token=share_token,
            is_public=True,
        )
        serializer = PublicRecipeSerializer(recipe)
        return Response(serializer.data)


class PublicRecipeRunView(APIView):
    """
    Public access to a shared recipe run.

    GET /api/recipes/runs/shared/{share_token}/
        No authentication required.
    """

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
