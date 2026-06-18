"""
API views for recipe management.
"""

import hashlib
import json
import logging
import time

from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus
from apps.recipes.services.runner import RecipeRunner, VariableValidationError
from apps.recipes.tasks import run_recipe
from apps.users.decorators import async_login_required
from apps.workspaces.services.workspace_service import touch_workspace_schemas
from apps.workspaces.workspace_resolver import aresolve_workspace
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
        workspace, _membership, err = resolve_workspace(request, workspace_id)
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
        workspace, _membership, err = resolve_workspace(request, workspace_id)
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


@csrf_protect
@async_login_required
async def recipe_run_view(request, workspace_id, recipe_id):
    """POST /api/workspaces/<workspace_id>/recipes/<recipe_id>/run/

    Execute a recipe with variable values. Raw async Django view (DRF APIView
    is sync and cannot await the async-first runner).
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user = request._authenticated_user

    workspace, err = await aresolve_workspace(user, workspace_id)
    if err:
        return err

    try:
        recipe = await Recipe.objects.select_related("workspace").aget(
            pk=recipe_id, workspace=workspace
        )
    except Recipe.DoesNotExist:
        return JsonResponse({"error": "Recipe not found."}, status=404)

    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    serializer = RunRecipeSerializer(data=body)
    if not await sync_to_async(serializer.is_valid)():
        return JsonResponse(serializer.errors, status=400)
    variable_values = serializer.validated_data.get("variable_values", {})

    # Validate variables (and apply defaults) synchronously so the client gets a
    # 400 in-band; only a valid request creates a run and dispatches work.
    try:
        values = RecipeRunner.validate_and_default(recipe, variable_values)
    except VariableValidationError as e:
        return JsonResponse({"error": str(e), "errors": e.errors}, status=400)

    # Execution is async: a recipe may block on a materialization (loading fresh
    # data before building its dashboard), which must not hold the HTTP request
    # open. Create the run PENDING, defer the background task, and return 202;
    # the client polls GET .../runs/<id>/ for progress and the final result.
    try:
        run = await RecipeRun.objects.acreate(
            recipe=recipe,
            run_by=user,
            status=RecipeRunStatus.PENDING,
            variable_values=values,
            step_results=[],
        )
        await run_recipe.defer_async(recipe_run_id=str(run.id))
    except Exception as e:
        # Don't leak internal exception detail to the client; log it behind a
        # short ref and return the ref (mirrors apps/chat/views.py).
        error_ref = hashlib.sha256(f"{time.time()}{e}".encode()).hexdigest()[:8]
        logger.exception("Error dispatching recipe %s [ref=%s]", recipe_id, error_ref)
        return JsonResponse({"error": f"Recipe run failed. Ref: {error_ref}"}, status=500)

    # Reset the inactivity TTL on user-initiated recipe runs.
    await touch_workspace_schemas(workspace)

    data = await sync_to_async(lambda: RecipeRunSerializer(run).data)()
    return JsonResponse(data, status=202)


class RecipeRunListView(APIView):
    """
    GET /api/recipes/<recipe_id>/runs/ - List runs for a recipe.
    """

    def get(self, request, workspace_id, recipe_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
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
        workspace, _membership, err = resolve_workspace(request, workspace_id)
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
