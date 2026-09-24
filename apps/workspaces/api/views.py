"""
API views for data dictionary and workspace schema management.
"""

import logging
from dataclasses import dataclass

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.error_codes import ErrorCode
from apps.common.localized import localized_str
from apps.knowledge.models import TableKnowledge
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    WorkspaceRole,
)
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    resolve_pipeline_config,
)
from apps.workspaces.services.refresh_requests import find_legacy_refresh_jobs
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection
from apps.workspaces.services.tenant_metadata import get_tenant_metadata
from apps.workspaces.tasks import refresh_tenant_schema, settle_finished_refresh_candidates
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


def _pipeline_unresolved_response(exc: PipelineResolutionError) -> Response:
    """503 for a tenant whose materialization pipeline cannot be resolved (#155).

    A degraded read rather than a 500: the schema itself may be healthy, Scout
    just cannot say which loader wrote it — and describing the tables with a
    guessed pipeline would attribute another provider's names and descriptions
    to this workspace's data. Logged because a supported provider with no
    pipeline is a deploy defect, not a workspace setting.
    """
    logger.exception("Pipeline resolution failed for the data dictionary")
    return Response(
        {
            "error": str(exc),
            "code": ErrorCode.PIPELINE_UNRESOLVED,
            "schema_status": "failed",
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


def _sync_pipeline_list_tables(tenant_schema, pipeline_config, live_table_names: set[str]) -> list:
    """Synchronous equivalent of ``pipeline_list_tables`` for the sync DRF view.

    Uses sync ORM and the already-fetched ``live_table_names`` so the request
    reads the managed DB once, avoiding the old async_to_sync event loop per
    request (arch #254, finding 10#2). Surfaces only ``completed`` sources whose
    physical table is present, plus dbt models that physically exist.
    """
    run = (
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
    if run is None:
        return []

    materialized_at = run.completed_at.isoformat() if run.completed_at else None
    sources_result = (run.result or {}).get("sources", {})
    source_descriptions = {s.name: s.description for s in pipeline_config.sources}
    source_physical_names = {s.name: s.physical_table_name for s in pipeline_config.sources}

    tables = []
    for source_name, source_data in sources_result.items():
        if (source_data or {}).get("state") != "completed":
            continue
        physical_name = source_physical_names.get(source_name, f"raw_{source_name}")
        if physical_name not in live_table_names:
            continue
        tables.append(
            {
                "name": physical_name,
                "type": "table",
                "description": source_descriptions.get(source_name, ""),
                "materialized_row_count": source_data.get("rows"),
                "row_count_verified": False,
                "materialized_at": materialized_at,
            }
        )

    for model_name in pipeline_config.dbt_models:
        if live_table_names and model_name not in live_table_names:
            continue
        tables.append(
            {
                "name": model_name,
                "type": "table",
                "description": "",
                "materialized_row_count": None,
                "row_count_verified": False,
                "materialized_at": materialized_at,
            }
        )

    return tables


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
                        "name": localized_str(ct.get("name", "")),
                        "app_name": localized_str(ct.get("app_name", "")),
                        "module_name": localized_str(ct.get("module_name", "")),
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
                        "name": localized_str(fd.get("name", xmlns)),
                        "app_name": localized_str(fd.get("app_name", "")),
                        "module_name": localized_str(fd.get("module_name", "")),
                        "case_type": localized_str(fd.get("case_type", "")),
                    }
                    for xmlns, fd in form_definitions.items()
                ],
            }

    return None


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

        try:
            pipeline_config = resolve_pipeline_config(tenant_schema, last_run)
        except PipelineResolutionError as exc:
            return _pipeline_unresolved_response(exc)

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

        tables_list = [
            t
            for t in _sync_pipeline_list_tables(tenant_schema, pipeline_config, live_table_names)
            if not t["name"].startswith("stg_")
        ]
        if not tables_list:
            return Response({"tables": {}, "generated_at": None})

        tenant_metadata = get_tenant_metadata(tenant_schema.tenant_id)
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


@dataclass(frozen=True)
class _RefreshOutcome:
    """One source's refresh outcome: its public entry, plus the single-source
    response (body and status) it would have produced on its own."""

    public: dict
    body: dict
    http_status: int


class RefreshSchemaView(APIView):
    """
    POST /api/workspaces/<workspace_id>/refresh/

    Triggers a background refresh of every source in the workspace. Requires
    read-write or manage role. Returns 202 Accepted once at least one source's
    refresh is queued; each source reports its own outcome.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
        if err:
            return err

        tenants = sorted(workspace.tenants.all(), key=lambda tenant: str(tenant.id))
        if not tenants:
            return Response(
                {"error": "Workspace has no associated tenant."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        memberships = {
            m.tenant_id: m
            for m in TenantMembership.objects.filter(user=request.user, tenant__in=tenants)
        }
        # Queue scans must not run under the tenant row locks (see find_legacy_refresh_jobs).
        legacy_jobs = {
            tenant.id: find_legacy_refresh_jobs(tenant)
            for tenant in tenants
            if tenant.id in memberships
        }

        outcomes = []
        with transaction.atomic():
            # One global order, so two refreshes over overlapping sources can't deadlock.
            locked = (
                Tenant.objects.select_for_update()
                .filter(id__in=[tenant.id for tenant in tenants])
                .order_by("id")
            )
            for tenant in locked:
                outcomes.append(
                    self._queue_tenant_refresh(
                        request, workspace, tenant, memberships.get(tenant.id), legacy_jobs
                    )
                )

        if len(outcomes) == 1:
            # Single-source workspaces keep the original response shapes.
            only = outcomes[0]
            if only.public["status"] == "provisioning":
                return Response(
                    {"schema_id": only.public["schema_id"], "status": "provisioning"},
                    status=status.HTTP_202_ACCEPTED,
                )
            return Response(only.body, status=only.http_status)
        started = any(o.public["status"] == "provisioning" for o in outcomes)
        # No top-level schema_id: each source reports its own in tenants[].
        body = {
            "status": "provisioning" if started else "not_started",
            "tenants": [o.public for o in outcomes],
        }
        if started:
            return Response(body, status=status.HTTP_202_ACCEPTED)
        codes = {o.body.get("code") for o in outcomes} - {None}
        if ErrorCode.REFRESH_RECOVERY_REQUIRED in codes:
            body["code"] = ErrorCode.REFRESH_RECOVERY_REQUIRED
        # 400 only when every source was a bad request, as the single-source path.
        http_status = (
            status.HTTP_400_BAD_REQUEST
            if all(o.http_status == status.HTTP_400_BAD_REQUEST for o in outcomes)
            else status.HTTP_409_CONFLICT
        )
        return Response(body, status=http_status)

    @staticmethod
    def _queue_tenant_refresh(
        request, workspace, tenant, tenant_membership, legacy_jobs
    ) -> _RefreshOutcome:
        source = {"tenant_id": str(tenant.id), "tenant_name": tenant.canonical_name}

        def refused(state, error, http_status, code=None):
            body = {"error": error, **({"code": code} if code else {})}
            return _RefreshOutcome({**source, "status": state, **body}, body, http_status)

        if tenant_membership is None:
            return refused(
                "no_membership",
                "No tenant membership found for this workspace.",
                status.HTTP_400_BAD_REQUEST,
            )
        legacy = settle_finished_refresh_candidates(tenant, legacy_jobs[tenant.id])
        if legacy.recovery_needed:
            return refused(
                "recovery_required",
                "A previous refresh could not be verified. Ask an operator to inspect "
                "and reconcile the queued refresh before retrying.",
                status.HTTP_409_CONFLICT,
                ErrorCode.REFRESH_RECOVERY_REQUIRED,
            )
        if (
            TenantSchema.objects.select_for_update()
            .filter(tenant=tenant, state=SchemaState.PROVISIONING)
            .exists()
        ):
            return refused(
                "in_progress", "A refresh is already in progress.", status.HTTP_409_CONFLICT
            )
        new_schema = SchemaManager().create_refresh_schema(tenant)
        job = refresh_tenant_schema.defer(
            schema_id=str(new_schema.id),
            membership_id=str(tenant_membership.id),
            actor_user_id=str(request.user.id),
            workspace_id=str(workspace.id),
        )
        new_schema.refresh_job_id = getattr(job, "id", job)
        new_schema.refresh_workspace_id = workspace.id
        new_schema.refresh_actor_user_id = request.user.id
        new_schema.refresh_membership_id = tenant_membership.id
        new_schema.save(
            update_fields=[
                "refresh_job_id",
                "refresh_workspace_id",
                "refresh_actor_user_id",
                "refresh_membership_id",
            ]
        )
        return _RefreshOutcome(
            {**source, "status": "provisioning", "schema_id": str(new_schema.id)},
            {},
            status.HTTP_202_ACCEPTED,
        )


class RefreshStatusView(APIView):
    """
    GET /api/workspaces/<workspace_id>/refresh/status/

    Returns the current schema state of each workspace source (and an aggregate).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        tenants = sorted(workspace.tenants.all(), key=lambda tenant: str(tenant.id))
        if not tenants:
            return Response({"state": "unavailable", "started_at": None, "error": None})
        statuses = [_latest_refresh_status(tenant) for tenant in tenants]
        if len(statuses) == 1:
            return Response({k: v for k, v in statuses[0].items() if k != "tenant_id"})
        # Truthful per source. The aggregate is the most severe state any source
        # is in, failure first, so it never contradicts the error beside it.
        states = {entry["state"] for entry in statuses}
        aggregate = next(
            (state for state in _AGGREGATE_STATE_ORDER if state in states), statuses[0]["state"]
        )
        started = [entry["started_at"] for entry in statuses if entry["started_at"]]
        return Response(
            {
                "state": aggregate,
                "started_at": max(started) if started else None,
                "error": next((e["error"] for e in statuses if e["error"]), None),
                "tenants": statuses,
            }
        )


# Every state a source can report, most severe first.
_AGGREGATE_STATE_ORDER = (
    SchemaState.FAILED,
    SchemaState.PROVISIONING,
    SchemaState.MATERIALIZING,
    "unavailable",
    SchemaState.TEARDOWN,
    SchemaState.EXPIRED,
    SchemaState.ACTIVE,
)


def _latest_refresh_status(tenant) -> dict:
    latest = TenantSchema.objects.filter(tenant=tenant).order_by("-created_at").first()
    if latest is None:
        return {
            "tenant_id": str(tenant.id),
            "state": "unavailable",
            "started_at": None,
            "error": None,
        }
    return {
        "tenant_id": str(tenant.id),
        "state": latest.state,
        "started_at": latest.created_at.isoformat(),
        "error": "Schema provisioning failed." if latest.state == SchemaState.FAILED else None,
    }


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
                    table_data = self._get_pipeline_table(tenant_schema, schema_name, table_name)
                    if table_data is not None:
                        return table_data

        # Fallback: legacy data_dictionary JSONField
        raw_dict = workspace.data_dictionary or {}
        return raw_dict.get("tables", {}).get(qualified_name)

    def _get_pipeline_table(self, tenant_schema, schema_name, table_name):
        """Return table data from pipeline models, or None if not found or hidden.

        Raises ``PipelineResolutionError`` when the tenant's pipeline is
        unresolvable: None here becomes a 404 "Table not found", which would be
        a lie about a table Scout simply cannot describe (#155).
        """
        if table_name.startswith("stg_"):
            return None

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
        pipeline_config = resolve_pipeline_config(tenant_schema, last_run)

        live_table_names = _live_tables_in_schema_sync(schema_name)
        known = {
            t["name"]
            for t in _sync_pipeline_list_tables(tenant_schema, pipeline_config, live_table_names)
        }
        if table_name not in known:
            return None

        tenant_metadata = get_tenant_metadata(tenant_schema.tenant_id)
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

        try:
            table_data = self._get_table_data(workspace, workspace.tenant, qualified_name)
        except PipelineResolutionError as exc:
            return _pipeline_unresolved_response(exc)
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

        try:
            table_data = self._get_table_data(workspace, workspace.tenant, qualified_name)
        except PipelineResolutionError as exc:
            return _pipeline_unresolved_response(exc)
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
