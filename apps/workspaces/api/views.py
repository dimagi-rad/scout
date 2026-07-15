"""
API views for data dictionary and workspace schema management.
"""

import logging

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.models import TableKnowledge
from apps.users.models import TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceRole,
)
from apps.workspaces.services.catalog import list_catalog_sync
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    resolve_pipeline_config_sync,
)
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection
from apps.workspaces.services.tenant_metadata import get_tenant_metadata_sync
from apps.workspaces.tasks import refresh_tenant_schema
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

logger = logging.getLogger(__name__)


def _resolve_tenant_schema(tenant):
    """Return the active TenantSchema for the given tenant, or None."""
    return TenantSchema.objects.filter(
        tenant=tenant,
        state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING],
    ).first()


def _schema_unavailable_response(tenant) -> Response | None:
    """Return a 503 Response if the workspace schema is not available, else None.

    Returns None when an ACTIVE or MATERIALIZING schema exists (data is readable).
    """
    if tenant is None:
        return Response(
            {
                "error": "Data unavailable. Please refresh workspace data.",
                "schema_status": "unavailable",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if TenantSchema.objects.filter(
        tenant=tenant, state__in=[SchemaState.ACTIVE, SchemaState.MATERIALIZING]
    ).exists():
        return None

    provisioning = TenantSchema.objects.filter(
        tenant=tenant,
        state__in=[SchemaState.PROVISIONING],
    ).exists()
    schema_status = "provisioning" if provisioning else "unavailable"
    return Response(
        {
            "error": "Data unavailable. Please refresh workspace data.",
            "schema_status": schema_status,
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _columns_from_conn(conn, schema_name: str) -> dict[str, list[dict]]:
    """Read all columns for *schema_name* using an already-open connection.

    Returns ``table_name -> list of column dicts``. Reuses the caller's
    connection so the data-dictionary request opens the managed DB once rather
    than per helper (arch #254, finding 10#2).
    """
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


def _live_tables_from_conn(conn, schema_name: str) -> set[str]:
    """Read the set of physical table names in *schema_name* from an open conn."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (schema_name,),
    )
    names = {row[0] for row in cursor.fetchall()}
    cursor.close()
    return names


def _get_all_columns(schema_name: str) -> dict[str, list[dict]]:
    """Query managed DB for columns of every table in *schema_name*.

    Returns a mapping of table_name → list of column dicts.
    Returns an empty dict on any connection error.
    """
    try:
        conn = get_managed_db_connection()
        try:
            return _columns_from_conn(conn, schema_name)
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to query managed DB for schema '%s'", schema_name)
        return {}


def _live_tables_in_schema_sync(schema_name: str) -> set[str]:
    """Read the live physical table names for a schema (single connection).

    Returns an empty set on any connection error (treated as "nothing live",
    matching the async ``_live_tables_in_schema``).
    """
    try:
        conn = get_managed_db_connection()
        try:
            return _live_tables_from_conn(conn, schema_name)
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to enumerate live tables in schema '%s'", schema_name)
        return set()


def _sync_pipeline_list_tables(
    tenant_schema, pipeline_config, live_table_names: set[str], workspace_id=None
) -> list:
    """Reconciled catalog for the sync DRF data dictionary.

    Delegates to the canonical catalog service (arch #251, Phase 3) so the
    dictionary returns the SAME table set as the agent prompt and the MCP tools —
    same source-of-truth, same fail-closed reconciliation (including terminal
    transformation assets), and the same ``stg_*`` policy. Stays sync and reuses
    the caller's already-fetched ``live_table_names`` so the request reads the
    managed DB once (no async_to_sync event loop per request, arch #254 10#2).
    """
    return list_catalog_sync(
        tenant_schema, pipeline_config, live_table_names, workspace_id=workspace_id
    )


def _get_table_columns(schema_name: str, table_name: str) -> list[dict]:
    """Query managed DB for columns of a single table.

    Returns an empty list on any connection error or if the table doesn't exist.
    """
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


def _get_tenant_metadata(tenant):
    """Return the canonical TenantMetadata for the tenant, or None.

    Delegates to the ONE deterministic, live-filtered read (arch #251, Phase 4,
    Decision 5) so the dictionary shows the same annotations as the prompt and
    the MCP tools.
    """
    return get_tenant_metadata_sync(tenant.id)


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


def _logical_table_name(qualified_name: str) -> str:
    """Return the stable logical table name (portion after the final ``.``).

    TableKnowledge is keyed on the logical name, not the physical schema-qualified
    one — the physical schema is regenerated each refresh, so keying on it would
    orphan annotations (arch #262, finding 01#5).
    """
    return qualified_name.rsplit(".", 1)[-1]


def _get_annotation(workspace, qualified_name):
    """Return serialized TableKnowledge annotation for a table, or None.

    Looks up by the stable logical table name (see _logical_table_name).
    """
    table_name = _logical_table_name(qualified_name)
    try:
        tk = TableKnowledge.objects.get(workspace=workspace, table_name=table_name)
        return _serialize_annotation(tk)
    except TableKnowledge.DoesNotExist:
        return None


def _get_annotations_by_logical_name(workspace) -> dict[str, dict]:
    """Return ``{logical_table_name: serialized annotation}`` for a workspace.

    One query instead of a per-table ``.get`` N+1 (arch #254, finding 10#2).
    """
    return {
        tk.table_name: _serialize_annotation(tk)
        for tk in TableKnowledge.objects.filter(workspace=workspace)
    }


class DataDictionaryView(APIView):
    """
    GET /api/data-dictionary/

    Returns the workspace's data dictionary merged with TableKnowledge annotations.
    Sources table metadata from the latest completed MaterializationRun and the
    managed database's information_schema.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        unavailable = _schema_unavailable_response(workspace.tenant)
        if unavailable is not None:
            return unavailable

        tenant_schema = _resolve_tenant_schema(workspace.tenant)
        if tenant_schema is None:
            return Response(
                {"schema_status": "unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return self._get_from_pipeline(workspace, tenant_schema)

    def _get_from_pipeline(self, workspace, tenant_schema):
        last_run = (
            MaterializationRun.objects.filter(
                tenant_schema=tenant_schema,
                state__in=[
                    MaterializationRun.RunState.COMPLETED,
                    MaterializationRun.RunState.PARTIAL,
                ],
            )
            .order_by("-completed_at")
            .first()
        )

        # Canonical pipeline resolution (arch #251, Phase 4 / #256): a truthful
        # error instead of silently serving commcare metadata for a non-commcare
        # tenant whose pipeline can't be resolved.
        try:
            pipeline_config = resolve_pipeline_config_sync(tenant_schema, last_run)
        except PipelineResolutionError:
            logger.warning(
                "No pipeline resolved for schema '%s'; data dictionary unavailable",
                tenant_schema.schema_name,
            )
            return Response(
                {
                    "error": "The data pipeline for this workspace could not be resolved.",
                    "schema_status": "pipeline_unresolved",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        schema_name = tenant_schema.schema_name

        # Read live-table set and all columns from ONE connection (arch #254,
        # finding 10#2). Degrade to an empty catalog on connection error.
        try:
            conn = get_managed_db_connection()
            try:
                live_table_names = _live_tables_from_conn(conn, schema_name)
                all_columns = _columns_from_conn(conn, schema_name)
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to query managed DB for schema '%s'", schema_name)
            live_table_names = set()
            all_columns = {}

        # The catalog service applies the stg_* policy (Decision 4a) itself, so no
        # per-view filter here.
        tables_list = _sync_pipeline_list_tables(
            tenant_schema, pipeline_config, live_table_names, workspace_id=workspace.id
        )
        if not tables_list:
            return Response({"tables": {}, "generated_at": None})

        tenant = tenant_schema.tenant
        tenant_metadata = _get_tenant_metadata(tenant)
        annotations = _get_annotations_by_logical_name(workspace)

        enriched_tables = {}
        for table_info in tables_list:
            table_name = table_info["name"]
            qualified_name = f"{schema_name}.{table_name}"
            annotation = annotations.get(_logical_table_name(qualified_name))
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


class RefreshSchemaView(APIView):
    """
    POST /api/workspaces/<workspace_id>/refresh/

    Triggers a background schema refresh. Requires read-write or manage role.
    Returns 202 Accepted immediately.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        if membership.role not in (WorkspaceRole.READ_WRITE, WorkspaceRole.MANAGE):
            return Response(
                {"error": "Read-write or manage role required to trigger a refresh."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tenant = workspace.tenant
        if tenant is None:
            return Response(
                {"error": "Workspace has no associated tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant_membership = TenantMembership.objects.filter(
            user=request.user, tenant=tenant
        ).first()
        if tenant_membership is None:
            return Response(
                {"error": "No tenant membership found for this workspace."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if (
                TenantSchema.objects.select_for_update()
                .filter(tenant=tenant, state=SchemaState.PROVISIONING)
                .exists()
            ):
                return Response(
                    {"error": "A refresh is already in progress."},
                    status=status.HTTP_409_CONFLICT,
                )
            new_schema = SchemaManager().create_refresh_schema(tenant)
            schema_id = str(new_schema.id)
            membership_id = str(tenant_membership.id)
            refresh_tenant_schema.defer(schema_id=schema_id, membership_id=membership_id)

        return Response(
            {"schema_id": schema_id, "status": "provisioning"},
            status=status.HTTP_202_ACCEPTED,
        )


class RefreshStatusView(APIView):
    """
    GET /api/workspaces/<workspace_id>/refresh/status/

    Returns the current schema state for the workspace's tenant.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        tenant = workspace.tenant
        if tenant is None:
            return Response({"state": "unavailable", "started_at": None, "error": None})

        latest = TenantSchema.objects.filter(tenant=tenant).order_by("-created_at").first()
        if latest is None:
            return Response({"state": "unavailable", "started_at": None, "error": None})

        error = "Schema provisioning failed." if latest.state == SchemaState.FAILED else None
        return Response(
            {
                "state": latest.state,
                "started_at": latest.created_at.isoformat(),
                "error": error,
            }
        )


class TableDetailView(APIView):
    """
    GET /api/data-dictionary/tables/<qualified_name>/
    PUT /api/data-dictionary/tables/<qualified_name>/
    """

    permission_classes = [IsAuthenticated]

    def _get_table_data(self, workspace, tenant, qualified_name):
        """Return table data dict, sourcing from pipeline models or legacy JSONField."""
        tenant_schema = _resolve_tenant_schema(tenant) if tenant else None
        if tenant_schema is not None:
            parts = qualified_name.split(".", 1)
            if len(parts) == 2:
                schema_name, table_name = parts
                if schema_name == tenant_schema.schema_name:
                    table_data = self._get_pipeline_table(
                        tenant_schema, schema_name, table_name, workspace_id=workspace.id
                    )
                    if table_data is not None:
                        return table_data

        # Fallback: legacy data_dictionary JSONField
        raw_dict = workspace.data_dictionary or {}
        return raw_dict.get("tables", {}).get(qualified_name)

    def _get_pipeline_table(self, tenant_schema, schema_name, table_name, workspace_id=None):
        """Return table data from pipeline models, or None if not found or hidden.

        Membership in the reconciled catalog (which applies the stg_* policy and
        fail-closed reconciliation) is the single gate — a stg_* or physically
        absent table simply isn't in ``known``.
        """
        last_run = (
            MaterializationRun.objects.filter(
                tenant_schema=tenant_schema,
                state__in=[
                    MaterializationRun.RunState.COMPLETED,
                    MaterializationRun.RunState.PARTIAL,
                ],
            )
            .order_by("-completed_at")
            .first()
        )
        # Canonical pipeline resolution (arch #251, Phase 4 / #256): if no pipeline
        # resolves, treat the table as not found rather than describing it with
        # wrong-provider (commcare) metadata.
        try:
            pipeline_config = resolve_pipeline_config_sync(tenant_schema, last_run)
        except PipelineResolutionError:
            logger.warning(
                "No pipeline resolved for schema '%s'; table '%s' treated as not found",
                schema_name,
                table_name,
            )
            return None

        live_table_names = _live_tables_in_schema_sync(schema_name)
        known = {
            t["name"]
            for t in _sync_pipeline_list_tables(
                tenant_schema, pipeline_config, live_table_names, workspace_id=workspace_id
            )
        }
        if table_name not in known:
            return None

        tenant = tenant_schema.tenant
        tenant_metadata = _get_tenant_metadata(tenant)
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

    def get(self, request, workspace_id, qualified_name):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        unavailable = _schema_unavailable_response(workspace.tenant)
        if unavailable is not None:
            return unavailable

        table_data = self._get_table_data(workspace, workspace.tenant, qualified_name)
        if table_data is None:
            return Response({"error": "Table not found."}, status=status.HTTP_404_NOT_FOUND)

        annotation = _get_annotation(workspace, qualified_name)
        response_data = dict(table_data)
        response_data["qualified_name"] = qualified_name
        if annotation:
            response_data["annotation"] = annotation

        return Response(response_data)

    def put(self, request, workspace_id, qualified_name):
        workspace, membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        if membership.role == WorkspaceRole.READ:
            return Response(
                {"error": "Read-write or manage role required to annotate tables."},
                status=status.HTTP_403_FORBIDDEN,
            )

        table_data = self._get_table_data(workspace, workspace.tenant, qualified_name)
        if table_data is None:
            return Response({"error": "Table not found."}, status=status.HTTP_404_NOT_FOUND)

        data = request.data

        def _to_list(value):
            if isinstance(value, list):
                return value
            if isinstance(value, str) and value.strip():
                return [line for line in value.splitlines() if line.strip()]
            return []

        # Key annotations by the stable logical table name so they survive a
        # schema refresh (arch #262, finding 01#5).
        tk, _ = TableKnowledge.objects.get_or_create(
            workspace=workspace,
            table_name=_logical_table_name(qualified_name),
            defaults={"description": "", "updated_by": request.user},
        )

        # Partial-update: only mutate a field whose key is in the payload. The
        # debounced autosave omits curated fields, so clobbering them with a
        # default would destroy admin-curated annotations (arch #262, finding 05#0).
        if "description" in data:
            tk.description = data.get("description") or ""
        if "use_cases" in data:
            tk.use_cases = _to_list(data.get("use_cases"))
        if "data_quality_notes" in data:
            tk.data_quality_notes = _to_list(data.get("data_quality_notes"))
        if "refresh_frequency" in data:
            tk.refresh_frequency = data.get("refresh_frequency") or ""
        if "owner" in data:
            tk.owner = data.get("owner") or ""
        if "related_tables" in data:
            related_tables = data.get("related_tables")
            if isinstance(related_tables, str):
                related_tables = [t.strip() for t in related_tables.split(",") if t.strip()]
            tk.related_tables = related_tables if isinstance(related_tables, list) else []
        if "column_notes" in data:
            column_notes = data.get("column_notes")
            tk.column_notes = column_notes if isinstance(column_notes, dict) else {}

        tk.updated_by = request.user
        tk.save()

        return Response(_serialize_annotation(tk))
