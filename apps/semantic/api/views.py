from __future__ import annotations

import json

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.semantic.models import SemanticCanvas, SemanticDataset
from apps.semantic.services.catalog import (
    SemanticCatalogUnavailable,
    get_active_semantic_model,
    serialize_catalog,
    serialize_dataset,
)
from apps.semantic.services.query import run_semantic_query_sync
from apps.workspaces.models import WorkspaceRole
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace


class DatasetListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        try:
            model = get_active_semantic_model(workspace)
        except SemanticCatalogUnavailable as exc:
            return Response(
                {"error": str(exc), "schema_status": exc.schema_status},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(serialize_catalog(model))


class DatasetDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id, dataset_name):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        try:
            model = get_active_semantic_model(workspace)
            dataset = model.datasets.get(name=dataset_name, is_visible=True)
        except SemanticCatalogUnavailable as exc:
            return Response(
                {"error": str(exc), "schema_status": exc.schema_status},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SemanticDataset.DoesNotExist:
            return Response({"error": "Dataset not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_dataset(dataset))


class SemanticQueryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        result = run_semantic_query_sync(workspace, request.data or {})
        if not result.get("success", True) or result.get("error"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)


class SemanticCanvasView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        try:
            model = get_active_semantic_model(workspace)
        except SemanticCatalogUnavailable as exc:
            return Response(
                {"error": str(exc), "schema_status": exc.schema_status},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        canvas = (
            SemanticCanvas.objects.filter(
                workspace=workspace,
                semantic_model=model,
                status=SemanticCanvas.Status.OPEN,
            )
            .order_by("-updated_at")
            .first()
        )
        if canvas is None:
            canvas = SemanticCanvas.objects.create(
                workspace=workspace,
                semantic_model=model,
                created_by=request.user,
            )
        return Response(_serialize_canvas(canvas, model))

    def post(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err
        if membership.role == WorkspaceRole.READ:
            return Response(
                {"error": "Read-write or manage role required to edit the semantic canvas."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            model = get_active_semantic_model(workspace)
        except SemanticCatalogUnavailable as exc:
            return Response(
                {"error": str(exc), "schema_status": exc.schema_status},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        canvas = (
            SemanticCanvas.objects.filter(
                workspace=workspace,
                semantic_model=model,
                status=SemanticCanvas.Status.OPEN,
            )
            .order_by("-updated_at")
            .first()
        )
        if canvas is None:
            canvas = SemanticCanvas(
                workspace=workspace,
                semantic_model=model,
                created_by=request.user,
            )
        canvas.changes = request.data.get("changes", canvas.changes)
        canvas.diagnostics = _diagnose_canvas_changes(canvas.changes)
        canvas.save()
        return Response(_serialize_canvas(canvas, model))


def _serialize_canvas(canvas: SemanticCanvas, model) -> dict:
    return {
        "id": str(canvas.id),
        "status": canvas.status,
        "changes": canvas.changes,
        "diagnostics": canvas.diagnostics,
        "catalog": serialize_catalog(model),
        "updated_at": canvas.updated_at.isoformat(),
    }


def _diagnose_canvas_changes(changes) -> list[dict]:
    if not isinstance(changes, dict):
        return [
            {
                "severity": "error",
                "code": "invalid_canvas_changes",
                "message": "Canvas changes must be an object.",
            }
        ]
    try:
        json.dumps(changes)
    except TypeError as exc:
        return [
            {
                "severity": "error",
                "code": "invalid_canvas_changes",
                "message": str(exc),
            }
        ]
    return []
