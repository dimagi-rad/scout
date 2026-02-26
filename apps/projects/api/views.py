"""
API views for data dictionary and workspace schema management.
"""

import logging

from django.db.models import F
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


def _resolve_membership(request):
    """Return the most-recently-selected TenantMembership for the authenticated user."""
    from apps.users.models import TenantMembership

    return (
        TenantMembership.objects.filter(user=request.user)
        .order_by(F("last_selected_at").desc(nulls_last=True))
        .first()
    )


def _resolve_workspace(request):
    """Resolve the active TenantWorkspace for the authenticated user."""
    from apps.projects.models import TenantWorkspace

    membership = _resolve_membership(request)
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


def _resolve_tenant_schema(membership):
    """Return the active TenantSchema for the given TenantMembership, or None.

    Matches by tenant_id rather than the specific membership FK so that multiple
    users in the same tenant see the same shared schema.
    """
    from apps.projects.models import SchemaState, TenantSchema

    return TenantSchema.objects.filter(
        tenant_membership__tenant_id=membership.tenant_id,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).first()


def _get_all_columns(schema_name: str) -> dict[str, list[dict]]:
    """Query managed DB for columns of every table in *schema_name*.

    Returns a mapping of table_name → list of column dicts.
    Returns an empty dict on any connection error.
    """
    from apps.projects.services.schema_manager import get_managed_db_connection

    try:
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT table_name, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = %s "
                "ORDER BY table_name, ordinal_position",
                (schema_name,),
            )
            rows = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to query managed DB for schema '%s'", schema_name)
        return {}

    columns_by_table: dict[str, list[dict]] = {}
    for table_name, col_name, data_type, is_nullable, default in rows:
        columns_by_table.setdefault(table_name, []).append(
            {
                "name": col_name,
                "data_type": data_type,
                "nullable": is_nullable == "YES",
                "default": default,
            }
        )
    return columns_by_table


def _get_table_columns(schema_name: str, table_name: str) -> list[dict]:
    """Query managed DB for columns of a single table.

    Returns an empty list on any connection error or if the table doesn't exist.
    """
    from apps.projects.services.schema_manager import get_managed_db_connection

    try:
        conn = get_managed_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema_name, table_name),
            )
            rows = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to query table '%s.%s'", schema_name, table_name)
        return []

    return [
        {"name": r[0], "data_type": r[1], "nullable": r[2] == "YES", "default": r[3]} for r in rows
    ]


def _localized_str(value) -> str:
    """Extract a plain string from a possibly-multilingual CommCare value.

    CommCare returns some fields as {"en": "Name"} dicts rather than plain strings.
    """
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), "") or ""
    return str(value) if value is not None else ""


def _build_source_metadata(table_name: str, tenant_metadata) -> dict | None:
    """Return structured source metadata for known tables derived from TenantMetadata.

    Returns None when no relevant metadata exists.
    """
    if tenant_metadata is None:
        return None

    metadata = tenant_metadata.metadata or {}

    if table_name == "cases":
        case_types = metadata.get("case_types", [])
        if case_types:
            return {
                "type": "case_types",
                "items": [
                    {
                        "name": _localized_str(ct.get("name", "")),
                        "app_name": _localized_str(ct.get("app_name", "")),
                        "module_name": _localized_str(ct.get("module_name", "")),
                    }
                    for ct in case_types
                ],
            }

    elif table_name == "forms":
        form_definitions = metadata.get("form_definitions", {})
        if form_definitions:
            return {
                "type": "form_definitions",
                "items": [
                    {
                        "name": _localized_str(fd.get("name", xmlns)),
                        "app_name": _localized_str(fd.get("app_name", "")),
                        "module_name": _localized_str(fd.get("module_name", "")),
                        "case_type": _localized_str(fd.get("case_type", "")),
                    }
                    for xmlns, fd in form_definitions.items()
                ],
            }

    return None


def _get_tenant_metadata(tenant_id: str):
    """Return TenantMetadata for any membership in the given tenant, or None."""
    from apps.projects.models import TenantMetadata

    return TenantMetadata.objects.filter(tenant_membership__tenant_id=tenant_id).first()


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
    Sources table metadata from the latest completed MaterializationRun and the
    managed database's information_schema.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        membership = _resolve_membership(request)
        tenant_schema = _resolve_tenant_schema(membership) if membership else None

        if tenant_schema is not None:
            return self._get_from_pipeline(workspace, tenant_schema)

        # Fallback: legacy data_dictionary JSONField (may be empty)
        return self._get_from_legacy(workspace)

    def _get_from_pipeline(self, workspace, tenant_schema):
        from apps.projects.models import MaterializationRun
        from mcp_server.pipeline_registry import get_registry
        from mcp_server.services.metadata import pipeline_list_tables

        last_run = (
            MaterializationRun.objects.filter(
                tenant_schema=tenant_schema,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .first()
        )

        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        tables_list = [
            t
            for t in pipeline_list_tables(tenant_schema, pipeline_config)
            if not t["name"].startswith("stg_")
        ]
        if not tables_list:
            return Response({"tables": {}, "generated_at": None})

        schema_name = tenant_schema.schema_name
        all_columns = _get_all_columns(schema_name)
        tenant_id = tenant_schema.tenant_membership.tenant_id
        tenant_metadata = _get_tenant_metadata(tenant_id)

        enriched_tables = {}
        for table_info in tables_list:
            table_name = table_info["name"]
            qualified_name = f"{schema_name}.{table_name}"
            annotation = _get_annotation(workspace, qualified_name)
            source_metadata = _build_source_metadata(table_name, tenant_metadata)
            entry = {
                "schema": schema_name,
                "name": table_name,
                "type": table_info.get("type", "table"),
                "columns": all_columns.get(table_name, []),
                "primary_key": [],
            }
            if source_metadata:
                entry["source_metadata"] = source_metadata
            if annotation:
                entry["annotation"] = annotation
            enriched_tables[qualified_name] = entry

        generated_at = last_run.completed_at if last_run else None
        return Response(
            {
                "tables": enriched_tables,
                "generated_at": generated_at.isoformat() if generated_at else None,
            }
        )

    def _get_from_legacy(self, workspace):
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

    def _get_table_data(self, workspace, membership, qualified_name):
        """Return table data dict, sourcing from pipeline models or legacy JSONField."""
        tenant_schema = _resolve_tenant_schema(membership) if membership else None
        if tenant_schema is not None:
            parts = qualified_name.split(".", 1)
            if len(parts) == 2:
                schema_name, table_name = parts
                if schema_name == tenant_schema.schema_name:
                    table_data = self._get_pipeline_table(tenant_schema, schema_name, table_name)
                    if table_data is not None:
                        return table_data

        # Fallback: legacy data_dictionary JSONField
        raw_dict = workspace.data_dictionary or {}
        return raw_dict.get("tables", {}).get(qualified_name)

    def _get_pipeline_table(self, tenant_schema, schema_name, table_name):
        """Return table data from pipeline models, or None if not found or hidden."""
        if table_name.startswith("stg_"):
            return None
        from apps.projects.models import MaterializationRun
        from mcp_server.pipeline_registry import get_registry
        from mcp_server.services.metadata import pipeline_list_tables

        last_run = (
            MaterializationRun.objects.filter(
                tenant_schema=tenant_schema,
                state=MaterializationRun.RunState.COMPLETED,
            )
            .order_by("-completed_at")
            .first()
        )
        pipeline_name = last_run.pipeline if last_run else "commcare_sync"
        pipeline_config = get_registry().get(pipeline_name) or get_registry().get("commcare_sync")

        known = {t["name"] for t in pipeline_list_tables(tenant_schema, pipeline_config)}
        if table_name not in known:
            return None

        tenant_id = tenant_schema.tenant_membership.tenant_id
        tenant_metadata = _get_tenant_metadata(tenant_id)
        source_metadata = _build_source_metadata(table_name, tenant_metadata)

        entry = {
            "schema": schema_name,
            "name": table_name,
            "type": "table",
            "columns": _get_table_columns(schema_name, table_name),
            "primary_key": [],
        }
        if source_metadata:
            entry["source_metadata"] = source_metadata
        return entry

    def get(self, request, qualified_name):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        membership = _resolve_membership(request)
        table_data = self._get_table_data(workspace, membership, qualified_name)
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

        membership = _resolve_membership(request)
        table_data = self._get_table_data(workspace, membership, qualified_name)
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
