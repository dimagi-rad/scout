"""
API views for data dictionary and workspace schema management.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


def _resolve_workspace(request):
    """Resolve the active TenantWorkspace for the authenticated user."""
    from django.db.models import F

    from apps.projects.models import TenantWorkspace
    from apps.users.models import TenantMembership

    membership = (
        TenantMembership.objects.filter(user=request.user)
        .order_by(F("last_selected_at").desc(nulls_last=True))
        .first()
    )
    if not membership:
        return None, Response(
            {"error": "No tenant selected. Please select a domain first."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    workspace, _ = TenantWorkspace.objects.get_or_create(
        tenant_id=membership.tenant_id,
        defaults={"tenant_name": membership.tenant_name},
    )
    return workspace, None


def _serialize_annotation(tk):
    """Serialize a TableKnowledge instance to the frontend annotation shape."""
    use_cases = tk.use_cases
    data_quality_notes = tk.data_quality_notes
    return {
        "description": tk.description,
        "use_cases": "\n".join(use_cases) if isinstance(use_cases, list) else (use_cases or ""),
        "data_quality_notes": "\n".join(data_quality_notes)
        if isinstance(data_quality_notes, list)
        else (data_quality_notes or ""),
        "refresh_frequency": tk.refresh_frequency,
        "owner": tk.owner,
        "related_tables": tk.related_tables or [],
        "column_notes": tk.column_notes or {},
    }


def _get_annotation(workspace, table_name):
    """Return serialized TableKnowledge annotation for a table, or None."""
    from apps.knowledge.models import TableKnowledge

    try:
        tk = TableKnowledge.objects.get(workspace=workspace, table_name=table_name)
        return _serialize_annotation(tk)
    except TableKnowledge.DoesNotExist:
        return None


class DataDictionaryView(APIView):
    """
    GET /api/data-dictionary/

    Returns the workspace's data dictionary merged with TableKnowledge annotations.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        raw_dict = workspace.data_dictionary or {}
        tables = raw_dict.get("tables", {})
        generated_at = workspace.data_dictionary_generated_at

        enriched_tables = {}
        for qualified_name, table_data in tables.items():
            annotation = _get_annotation(workspace, qualified_name)
            enriched = dict(table_data)
            if annotation:
                enriched["annotation"] = annotation
            enriched_tables[qualified_name] = enriched

        return Response(
            {
                "tables": enriched_tables,
                "generated_at": generated_at.isoformat() if generated_at else None,
            }
        )


class RefreshSchemaView(APIView):
    """
    POST /api/refresh-schema/

    Triggers a schema refresh for the active workspace.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        # Schema refresh is handled by the MCP server during agent interactions.
        # This endpoint acknowledges the request; future work can trigger an explicit refresh.
        return Response({"status": "ok"})


class TableDetailView(APIView):
    """
    GET /api/data-dictionary/tables/<qualified_name>/
    PUT /api/data-dictionary/tables/<qualified_name>/
    """

    permission_classes = [IsAuthenticated]

    def _get_table_data(self, workspace, qualified_name):
        raw_dict = workspace.data_dictionary or {}
        return raw_dict.get("tables", {}).get(qualified_name)

    def get(self, request, qualified_name):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        table_data = self._get_table_data(workspace, qualified_name)
        if table_data is None:
            return Response({"error": "Table not found."}, status=status.HTTP_404_NOT_FOUND)

        annotation = _get_annotation(workspace, qualified_name)
        response_data = dict(table_data)
        response_data["qualified_name"] = qualified_name
        if annotation:
            response_data["annotation"] = annotation

        return Response(response_data)

    def put(self, request, qualified_name):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        table_data = self._get_table_data(workspace, qualified_name)
        if table_data is None:
            return Response({"error": "Table not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.knowledge.models import TableKnowledge

        data = request.data

        # Convert string fields to list for storage in JSONField
        def _to_list(value):
            if isinstance(value, list):
                return value
            if isinstance(value, str) and value.strip():
                return [line for line in value.splitlines() if line.strip()]
            return []

        related_tables = data.get("related_tables", [])
        if isinstance(related_tables, str):
            related_tables = [t.strip() for t in related_tables.split(",") if t.strip()]

        tk, _ = TableKnowledge.objects.get_or_create(
            workspace=workspace,
            table_name=qualified_name,
            defaults={"description": "", "updated_by": request.user},
        )
        tk.description = data.get("description", tk.description)
        tk.use_cases = _to_list(data.get("use_cases", ""))
        tk.data_quality_notes = _to_list(data.get("data_quality_notes", ""))
        tk.refresh_frequency = data.get("refresh_frequency", tk.refresh_frequency)
        tk.owner = data.get("owner", tk.owner)
        tk.related_tables = related_tables
        column_notes = data.get("column_notes", {})
        tk.column_notes = column_notes if isinstance(column_notes, dict) else {}
        tk.updated_by = request.user
        tk.save()

        return Response(_serialize_annotation(tk))
