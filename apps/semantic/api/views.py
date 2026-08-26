from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chat.models import Thread
from apps.semantic.canvas import (
    apply_operations,
    canvas_projection,
    commit_canvas,
    resolve_thread_canvas,
)
from apps.semantic.models import SemanticDataset
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


def _resolve_thread_canvas(request, workspace_id, thread_id, *, write: bool):
    """Shared auth + resolution for the thread canvas endpoints."""
    workspace, membership, err = resolve_workspace(request, workspace_id)
    if err:
        return None, err
    if write and membership.role == WorkspaceRole.READ:
        return None, Response(
            {"error": "Read-write or manage role required to edit the canvas."},
            status=status.HTTP_403_FORBIDDEN,
        )
    thread = Thread.objects.filter(id=thread_id).first()
    if thread is not None and (
        thread.workspace_id != workspace.id or thread.user_id != request.user.id
    ):
        return None, Response({"error": "Thread not found."}, status=status.HTTP_404_NOT_FOUND)
    if thread is None:
        # Frontend-generated thread UUIDs may reach the canvas before the
        # first chat message creates the Thread row; create the shell.
        thread = Thread.objects.create(id=thread_id, workspace=workspace, user=request.user)
    try:
        canvas = resolve_thread_canvas(workspace, thread, request.user)
    except SemanticCatalogUnavailable as exc:
        return None, Response(
            {"error": str(exc), "schema_status": exc.schema_status},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return canvas, None


class ThreadCanvasView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id, thread_id):
        canvas, err = _resolve_thread_canvas(request, workspace_id, thread_id, write=False)
        if err:
            return err
        return Response(canvas_projection(canvas))


class ThreadCanvasApplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, thread_id):
        canvas, err = _resolve_thread_canvas(request, workspace_id, thread_id, write=True)
        if err:
            return err
        operations = (request.data or {}).get("operations")
        result = apply_operations(canvas, operations, request.user)
        if "errors" in result:
            # Top-level "error" so the frontend ApiError surfaces the message.
            first = result["errors"][0]
            return Response(
                {**result, "error": first.get("message", "Invalid canvas operation.")},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(canvas_projection(canvas))


class ThreadCanvasCommitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, thread_id):
        canvas, err = _resolve_thread_canvas(request, workspace_id, thread_id, write=True)
        if err:
            return err
        report = commit_canvas(canvas, request.user)
        report["projection"] = canvas_projection(canvas)
        return Response(report)
