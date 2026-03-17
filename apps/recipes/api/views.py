"""
API views for recipe management.
"""

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.recipes.models import Recipe, RecipeRun
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

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


class RecipeListView(APIView):
    """
    GET /api/recipes/ - List recipes for the active workspace.
    """

    def get(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        recipes = Recipe.objects.filter(workspace=workspace)
        serializer = RecipeListSerializer(recipes, many=True)
        return Response(serializer.data)


class RecipeDetailView(APIView):
    """
    GET    /api/recipes/<recipe_id>/ - Retrieve a recipe.
    PUT    /api/recipes/<recipe_id>/ - Update a recipe.
    DELETE /api/recipes/<recipe_id>/ - Delete a recipe.
    """

    def _get_recipe(self, request, workspace_id, recipe_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return None, err
        try:
            recipe = Recipe.objects.get(pk=recipe_id, workspace=workspace)
        except Recipe.DoesNotExist:
            return None, Response({"error": "Recipe not found."}, status=status.HTTP_404_NOT_FOUND)
        return recipe, None

    def get(self, request, workspace_id, recipe_id):
        recipe, err = self._get_recipe(request, workspace_id, recipe_id)
        if err:
            return err
        return Response(RecipeDetailSerializer(recipe).data)

    def put(self, request, workspace_id, recipe_id):
        recipe, err = self._get_recipe(request, workspace_id, recipe_id)
        if err:
            return err
        serializer = RecipeUpdateSerializer(recipe, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(RecipeDetailSerializer(recipe).data)

    def delete(self, request, workspace_id, recipe_id):
        recipe, err = self._get_recipe(request, workspace_id, recipe_id)
        if err:
            return err
        recipe.soft_delete(deleted_by=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RecipeRunView(APIView):
    """
    POST /api/recipes/<recipe_id>/run/ - Execute a recipe with variable values.
    """

    def post(self, request, workspace_id, recipe_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        try:
            recipe = Recipe.objects.get(pk=recipe_id, workspace=workspace)
        except Recipe.DoesNotExist:
            return Response({"error": "Recipe not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = RunRecipeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        variable_values = serializer.validated_data.get("variable_values", {})

        try:
            from apps.recipes.services.runner import RecipeRunner

            runner = RecipeRunner(recipe=recipe, variable_values=variable_values, user=request.user)
            run = runner.execute()
        except Exception as e:
            logger.exception("Error running recipe %s", recipe_id)
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Touch the schema to reset the inactivity TTL on user-initiated recipe runs
        from apps.workspaces.models import SchemaState, TenantSchema

        tenant = workspace.tenant
        if tenant:
            ts = TenantSchema.objects.filter(
                tenant=tenant, state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING]
            ).first()
            if ts is not None:
                ts.touch()

        return Response(RecipeRunSerializer(run).data, status=status.HTTP_201_CREATED)


class RecipeRunListView(APIView):
    """
    GET /api/recipes/<recipe_id>/runs/ - List runs for a recipe.
    """

    def get(self, request, workspace_id, recipe_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        try:
            recipe = Recipe.objects.get(pk=recipe_id, workspace=workspace)
        except Recipe.DoesNotExist:
            return Response({"error": "Recipe not found."}, status=status.HTTP_404_NOT_FOUND)
        runs = RecipeRun.objects.filter(recipe=recipe).order_by("-created_at")
        return Response(RecipeRunSerializer(runs, many=True).data)


class RecipeRunDetailView(APIView):
    """
    PATCH /api/recipes/<recipe_id>/runs/<run_id>/ - Update run sharing settings.
    """

    def patch(self, request, workspace_id, recipe_id, run_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        try:
            recipe = Recipe.objects.get(pk=recipe_id, workspace=workspace)
        except Recipe.DoesNotExist:
            return Response({"error": "Recipe not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            run = RecipeRun.objects.get(pk=run_id, recipe=recipe)
        except RecipeRun.DoesNotExist:
            return Response({"error": "Run not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = RecipeRunUpdateSerializer(run, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(RecipeRunSerializer(run).data)


class PublicRecipeRunView(APIView):
    """Public access to a shared recipe run."""

    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]

    def get(self, request, share_token):
        from django.shortcuts import get_object_or_404

        run = get_object_or_404(
            RecipeRun,
            share_token=share_token,
            is_public=True,
        )
        serializer = PublicRecipeRunSerializer(run)
        return Response(serializer.data)
