"""Semantic catalog bootstrap and serialization."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from asgiref.sync import async_to_sync
from django.db import models, transaction
from django.utils import timezone

from apps.common.capacity import reraise_if_capacity
from apps.common.error_codes import ErrorCode
from apps.common.identifiers import view_name
from apps.knowledge.models import TableKnowledge
from apps.semantic.models import (
    CustomDataset,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from apps.semantic.services.column_policy import (
    HIDDEN_METADATA_KEY,
    column_hidden_reason,
    is_listed,
)
from apps.semantic.services.cube import DROPPED_JOIN_CODES
from apps.semantic.services.custom_datasets import (
    CustomDatasetError,
    compile_custom_dataset_sql,
    infer_custom_dataset_columns,
)
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceViewSchema,
)
from apps.workspaces.services.load_activity import workspace_schema_statuses
from apps.workspaces.services.pipeline_resolver import (
    PipelineResolutionError,
    aresolve_pipeline_config,
)
from apps.workspaces.services.query_state import synced_runs
from apps.workspaces.services.schema_manager import SchemaManager
from apps.workspaces.services.tenant_metadata import aget_tenant_metadata
from apps.workspaces.services.view_sources import (
    ViewSourcesError,
    parse_view_sources,
    validate_published_views,
)
from mcp_server.context import load_workspace_context
from mcp_server.event_time import SOURCE_TIME_COLUMNS, event_time_metadata, event_time_sql
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.metadata import (
    pipeline_describe_table,
    pipeline_list_tables,
    pipeline_table_primary_keys,
    workspace_list_tables,
)
from mcp_server.source_identity import source_identity, unverified_source_identity


class SemanticCatalogUnavailable(Exception):
    """Raised when no queryable schema is available for a workspace."""

    def __init__(
        self,
        message: str,
        schema_status: str = "unavailable",
        code: ErrorCode = ErrorCode.VALIDATION_ERROR,
    ) -> None:
        super().__init__(message)
        self.schema_status = schema_status
        self.code = code


@dataclass(frozen=True)
class PhysicalTable:
    name: str
    type: str
    description: str
    columns: list[dict[str, Any]]
    materialized_row_count: int | None = None
    materialized_at: str | None = None
    primary_key: str = ""
    source_tenant_ids: tuple[str, ...] = ()
    identity: dict[str, Any] | None = None
    source_table_name: str = ""
    provider: str = ""


_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")
_INTEGER_TYPES = {"smallint", "integer", "bigint", "smallserial", "serial", "bigserial"}
_NUMERIC_TYPES = {
    *_INTEGER_TYPES,
    "decimal",
    "numeric",
    "real",
    "double precision",
    "money",
}
_DISPLAY_METADATA_KEYS = {"format", "currency"}
_CURRENCY_COLUMN_TOKENS = {
    "amount",
    "balance",
    "budget",
    "charge",
    "charges",
    "cost",
    "earned",
    "fee",
    "fees",
    "paid",
    "payment",
    "price",
    "revenue",
    "salary",
    "spend",
    "usd",
    "wage",
    "wages",
}
_CURRENCY_CODES_BY_TOKEN = {
    "aud": "AUD",
    "cad": "CAD",
    "dollar": "USD",
    "dollars": "USD",
    "eur": "EUR",
    "gbp": "GBP",
    "inr": "INR",
    "jpy": "JPY",
    "usd": "USD",
}
_PERCENT_COLUMN_TOKENS = {"percent", "percentage", "pct", "rate", "ratio", "share"}
_TIME_TYPES = {
    "date",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone",
    "time",
    "time without time zone",
    "time with time zone",
}


def semantic_name(value: str, *, fallback: str = "field") -> str:
    """Return a stable slug safe for semantic member names."""
    normalized = _SAFE_NAME_RE.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        normalized = fallback
    if normalized[0].isdigit():
        normalized = f"{fallback}_{normalized}"
    return normalized[:255]


def _humanize_name(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _is_numeric(data_type: str) -> bool:
    return data_type.lower() in _NUMERIC_TYPES


def _is_integer(data_type: str) -> bool:
    return data_type.lower() in _INTEGER_TYPES


def _is_identifier_column(column_name: str, dataset: SemanticDataset) -> bool:
    """Identifier-ish columns get no sum/avg measures — those aggregates are noise."""
    lowered = column_name.lower()
    return lowered == "id" or lowered.endswith("_id") or column_name == dataset.primary_key


def _is_time(data_type: str) -> bool:
    lowered = data_type.lower()
    return lowered in _TIME_TYPES or "timestamp" in lowered


def _fallback_primary_key(table_type: str, columns: list[dict[str, Any]]) -> str:
    """Assume an ``id`` column keys a base table whose DDL declared no PK.

    Cube requires primary keys on joinable cubes. Views are excluded: a
    multi-tenant view unions per-tenant surrogate ids that can collide, so a
    view-level ``id`` is not a safe key.
    """
    if table_type != "table":
        return ""
    if any(column.get("name") == "id" for column in columns):
        return "id"
    return ""


def _column_tokens(column_name: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", column_name.lower()) if token}


def _infer_currency(column_name: str) -> str:
    for token in _column_tokens(column_name):
        code = _CURRENCY_CODES_BY_TOKEN.get(token)
        if code:
            return code
    return ""


def _infer_display_metadata(
    column_name: str,
    data_type: str,
    *,
    measure_type: str = "",
) -> dict[str, str]:
    """Infer Cube display metadata from the raw column name/type.

    Cube's native vocabulary is named formats like ``number_2`` and
    ``currency_2`` plus an optional ISO currency code. We keep the inference
    conservative: identifiers stay unformatted, money-ish names get currency
    formatting, and generic decimals get two decimal places.
    """
    if measure_type == SemanticField.MeasureType.COUNT:
        return {"format": "number_0"}
    if not _is_numeric(data_type):
        return {}

    tokens = _column_tokens(column_name)
    lowered_type = data_type.lower()
    if tokens & _PERCENT_COLUMN_TOKENS:
        return {"format": "percent_1"}
    if lowered_type == "money" or tokens & _CURRENCY_COLUMN_TOKENS:
        metadata = {"format": "currency_2"}
        currency = _infer_currency(column_name)
        if currency:
            metadata["currency"] = currency
        return metadata
    if _is_integer(data_type) and measure_type != SemanticField.MeasureType.AVG:
        return {"format": "number_0"}
    return {"format": "number_2"}


async def _load_physical_tables_async(workspace) -> tuple[str, list[PhysicalTable]]:
    # Can run inside the Cube refresh transaction, whose thread its ORM calls route back to.
    ctx = await load_workspace_context(str(workspace.id), touch=False)
    schema_name = ctx.schema_name

    view_schema = await WorkspaceViewSchema.objects.filter(
        workspace_id=workspace.id,
        schema_name=schema_name,
        state=SchemaState.ACTIVE,
    ).afirst()
    is_view_schema = view_schema is not None

    ts = None
    if not is_view_schema:
        ts = (
            await TenantSchema.objects.select_related("tenant")
            .filter(schema_name=schema_name)
            .afirst()
        )

    if ts is None:
        # A multi-tenant ws_* view schema (or a schema with no TenantSchema row)
        # has no single tenant, so there is no pipeline to attribute: read the
        # tables from information_schema and carry no pipeline descriptions.
        table_entries = await workspace_list_tables(ctx)
        pipeline_config = None
        tenant_metadata = None
    else:
        last_run = await synced_runs().filter(tenant_schema=ts).afirst()
        pipeline_config = await aresolve_pipeline_config(ts, last_run)
        table_entries = await pipeline_list_tables(ts, pipeline_config)
        tenant_metadata = await aget_tenant_metadata(ts.tenant_id)

    primary_keys = await pipeline_table_primary_keys(ctx)
    tenants = [tenant async for tenant in workspace.tenants.all()] if is_view_schema else []
    sources = (
        parse_view_sources(view_schema.view_sources, {str(tenant.id) for tenant in tenants})
        if view_schema is not None
        else None
    )
    if sources is not None:
        validate_published_views(sources, {entry.get("name", "") for entry in table_entries})
    physical_tables: list[PhysicalTable] = []
    for entry in table_entries:
        table_name = entry.get("name", "")
        if not table_name or table_name.startswith("stg_"):
            continue
        detail = await pipeline_describe_table(
            table_name,
            ctx,
            tenant_metadata,
            pipeline_config,
        )
        columns = (detail or {}).get("columns", [])
        source = sources.get(table_name) if sources else None
        owner = (
            ts.tenant
            if ts is not None
            else next(
                (tenant for tenant in tenants if source and str(tenant.id) == source.tenant_id),
                None,
            )
        )
        source_provider = owner.provider if owner else None
        source_table = sources[table_name].source_table_name if sources else table_name
        time_columns = (
            SOURCE_TIME_COLUMNS.get(owner.provider, {}).get(source_table, set()) if owner else set()
        )
        columns = [
            {
                **column,
                "event_time": event_time_metadata(owner.provider),
            }
            if column.get("name") in time_columns
            else column
            for column in columns
        ]
        physical_tables.append(
            PhysicalTable(
                name=table_name,
                type=entry.get("type", "table"),
                description=(detail or {}).get("description") or entry.get("description", ""),
                columns=columns,
                identity=(detail or {}).get("identity")
                or (
                    source_identity(source_provider, source_table, columns)
                    if source_provider is not None and detail is not None
                    else unverified_source_identity()
                ),
                materialized_row_count=entry.get("materialized_row_count"),
                materialized_at=entry.get("materialized_at"),
                primary_key=primary_keys.get(table_name, "")
                or _fallback_primary_key(entry.get("type", "table"), columns),
                source_tenant_ids=(
                    (str(ts.tenant_id),)
                    if ts is not None
                    else (sources[table_name].tenant_id,)
                    if sources is not None
                    else SchemaManager().tenant_ids_for_view(table_name, tenants)
                ),
                source_table_name=(
                    table_name
                    if ts is not None
                    else sources[table_name].source_table_name
                    if sources is not None
                    else ""
                ),
                provider=source_provider or "",
            )
        )
    return schema_name, physical_tables


# Why a workspace's tables could not be read, by its status. One that serves
# ("unavailable" here) failed for some other reason, so no diagnosis is claimed.
_UNREADABLE_CATALOG_CODES = {
    "not_loaded": ErrorCode.DATA_NOT_LOADED,
    "provisioning": ErrorCode.DATA_NOT_LOADED,
    "failed": ErrorCode.SCHEMA_BUILD_FAILED,
    "unavailable": ErrorCode.INTERNAL_ERROR,
}


def load_physical_tables(workspace) -> tuple[str, list[PhysicalTable]]:
    try:
        return async_to_sync(_load_physical_tables_async)(workspace)
    except PipelineResolutionError as exc:
        # Fail the build rather than describing the tables with a guessed
        # pipeline: this model gets cached and promoted to ACTIVE, so a wrong
        # guess outlives the request. Reported as its own message because
        # "refresh workspace data" cannot fix a missing pipeline (#155).
        raise SemanticCatalogUnavailable(
            str(exc), schema_status="failed", code=ErrorCode.PIPELINE_UNRESOLVED
        ) from exc
    except ViewSourcesError as exc:
        raise SemanticCatalogUnavailable(
            "The workspace view source map is invalid. Rebuild the query layer.",
            schema_status="failed",
            code=ErrorCode.SCHEMA_BUILD_FAILED,
        ) from exc
    except Exception as exc:
        # Retryable, not a data problem: "refresh workspace data" would send the
        # user to re-materialize because the DB merely refused a connection.
        reraise_if_capacity(exc)
        schema_status = workspace_schema_statuses([workspace.id])[workspace.id]
        if schema_status == "available":
            # Serving by its schema rows, yet its tables could not be read.
            schema_status = "unavailable"
        raise SemanticCatalogUnavailable(
            "Data unavailable. Please refresh workspace data.",
            schema_status=schema_status,
            code=_UNREADABLE_CATALOG_CODES.get(schema_status, ErrorCode.INTERNAL_ERROR),
        ) from exc


def _apply_curation(existing, defaults: dict[str, Any]) -> dict[str, Any]:
    """Respect curated keys and sticky metadata when refreshing an existing row.

    Canvas commits record user-curated keys in ``metadata.curated_fields``; a
    catalog refresh must not overwrite those values, and must carry the
    curation marker (and the canvas ``source`` marker) into the replacement
    metadata — otherwise the next materialization silently undoes the edits.
    """
    if existing is None:
        return defaults
    metadata = existing.metadata or {}
    curated = set(metadata.get("curated_fields", []))
    for key in curated:
        if key in defaults:
            defaults[key] = getattr(existing, key, defaults[key])
    if "metadata" in defaults:
        new_metadata = dict(defaults["metadata"])
        if curated:
            new_metadata["curated_fields"] = sorted(curated)
        if metadata.get("source"):
            new_metadata["source"] = metadata["source"]
        for key in _DISPLAY_METADATA_KEYS:
            if key in curated:
                if key in metadata:
                    new_metadata[key] = metadata[key]
                else:
                    new_metadata.pop(key, None)
        defaults["metadata"] = new_metadata
    return defaults


_BULK_BATCH_SIZE = 500


def _assign_changes(row: models.Model, values: dict[str, Any]) -> list[str]:
    """Set each differing value on ``row`` and return the names that changed.

    JSON values are compared in their stored form (tuples come back as lists),
    and relations by id so the comparison never fetches the related row.
    """
    changed = []
    for key, value in values.items():
        field = row._meta.get_field(key)
        attr = key
        if field.is_relation:
            current = getattr(row, field.attname)
            new = getattr(value, "pk", value)
            if not isinstance(value, models.Model):
                attr = field.attname
        elif isinstance(field, models.JSONField):
            current = getattr(row, key)
            value = new = json.loads(json.dumps(value))
        else:
            current, new = getattr(row, key), value
        if current != new:
            setattr(row, attr, value)
            changed.append(key)
    return changed


class _RowWriter:
    """Collect inserts and changed rows of one model, then write them in bulk.

    A catalog refresh rewrites thousands of rows that are almost all unchanged;
    a per-row ``update_or_create`` cost two round trips each (#918).
    """

    def __init__(self, model_class: type[models.Model]) -> None:
        self._model_class = model_class
        self._creates: dict[Any, models.Model] = {}
        self._updates: dict[Any, models.Model] = {}
        self._update_fields: set[str] = set()

    def upsert(self, existing, values: dict[str, Any], **lookup):
        if existing is None:
            row = self._model_class(**lookup, **values)
            self._creates[row.pk] = row
            return row
        changed = _assign_changes(existing, values)
        if changed and existing.pk not in self._creates:
            self._updates[existing.pk] = existing
            self._update_fields.update(changed)
        return existing

    def include_in_pending_write(self, row, field_name: str) -> bool:
        """Add ``field_name`` to ``row``'s pending write; False when none is pending."""
        if row.pk in self._creates:
            return True
        if row.pk in self._updates:
            self._update_fields.add(field_name)
            return True
        return False

    def flush(self) -> None:
        self._model_class.objects.bulk_create(
            list(self._creates.values()), batch_size=_BULK_BATCH_SIZE
        )
        if self._updates:
            # bulk_update skips auto_now; canvas conflict detection fingerprints
            # rows by updated_at, so a changed row must still move it.
            now = timezone.now()
            for row in self._updates.values():
                row.updated_at = now
            # One column list for every row: safe only because each row's other
            # values come from the same locked read.
            self._model_class.objects.bulk_update(
                list(self._updates.values()),
                [*sorted(self._update_fields), "updated_at"],
                batch_size=_BULK_BATCH_SIZE,
            )
        self._creates.clear()
        self._updates.clear()
        self._update_fields.clear()


def ensure_semantic_model(workspace) -> SemanticModel:
    """Create or refresh the default semantic catalog from active physical tables."""
    schema_name, tables = load_physical_tables(workspace)
    if not tables:
        raise SemanticCatalogUnavailable(
            "No queryable datasets are available.", code=ErrorCode.DATA_NOT_LOADED
        )

    with transaction.atomic():
        model, created = SemanticModel.objects.select_for_update().get_or_create(
            workspace=workspace,
            defaults={"name": f"{workspace.name} Semantic Model"},
        )
        existing_physical_dataset_ids: set[str] = set()
        annotations = {
            annotation.table_name: annotation
            for annotation in TableKnowledge.objects.filter(
                workspace=workspace, table_name__in=[table.name for table in tables]
            )
        }
        # Row locks, as update_or_create took: a canvas commit (which skips the
        # model lock) must not land between this read and the bulk write.
        datasets_by_name = {
            dataset.name: dataset
            for dataset in SemanticDataset.objects.select_for_update().filter(
                workspace=workspace,
                name__in={semantic_name(table.name, fallback="dataset") for table in tables},
            )
        }
        dataset_writer = _RowWriter(SemanticDataset)
        field_sync = _FieldSync(datasets_by_name.values())
        for table in tables:
            dataset_name = semantic_name(table.name, fallback="dataset")
            annotation = annotations.get(table.name)
            existing_dataset = datasets_by_name.get(dataset_name)
            defaults = _apply_curation(
                existing_dataset,
                {
                    "semantic_model": model,
                    "label": _humanize_name(table.name),
                    "description": (
                        annotation.description
                        if annotation and annotation.description
                        else table.description
                    ),
                    "source_kind": SemanticDataset.SourceKind.PHYSICAL,
                    "custom_dataset": None,
                    "schema_name": schema_name,
                    "table_name": table.name,
                    "primary_key": table.primary_key,
                    "row_count": table.materialized_row_count,
                    "is_visible": True,
                    "metadata": {
                        "source_type": table.type,
                        "materialized_at": table.materialized_at,
                        "row_count_verified": False,
                        **({"identity": table.identity} if table.identity else {}),
                        **(
                            {"source_table_name": table.source_table_name}
                            if table.source_table_name
                            else {}
                        ),
                        **(
                            {"source_tenant_ids": list(table.source_tenant_ids)}
                            if table.source_tenant_ids
                            else {}
                        ),
                    },
                },
            )
            dataset = dataset_writer.upsert(
                existing_dataset, defaults, workspace=workspace, name=dataset_name
            )
            datasets_by_name[dataset_name] = dataset
            existing_physical_dataset_ids.add(str(dataset.id))
            _derive_fields(field_sync, dataset, table.columns, annotation, provider=table.provider)
        dataset_writer.flush()
        field_sync.flush()

        SemanticDataset.objects.filter(
            workspace=workspace,
            source_kind=SemanticDataset.SourceKind.PHYSICAL,
        ).exclude(id__in=existing_physical_dataset_ids).update(is_visible=False)

        diagnostics = _sync_custom_datasets(model, workspace, schema_name)
        diagnostics.extend(_sync_relationships(model, workspace))
        model.status = SemanticModel.Status.ACTIVE
        model.diagnostics = diagnostics
        model.metadata = {**(model.metadata or {}), "catalog_diagnostics": diagnostics}
        if not created:
            model.version += 1
        model.save(update_fields=["version", "status", "diagnostics", "metadata", "updated_at"])
        return model


def _active_semantic_models(workspace):
    return SemanticModel.objects.filter(workspace=workspace, status=SemanticModel.Status.ACTIVE)


def no_active_semantic_model() -> SemanticCatalogUnavailable:
    return SemanticCatalogUnavailable(
        "No active semantic model is available.",
        schema_status="unavailable",
        code=ErrorCode.SEMANTIC_MODEL_UNAVAILABLE,
    )


def _require_active_semantic_model(model) -> SemanticModel:
    if model is None:
        raise no_active_semantic_model()
    return model


def get_active_semantic_model(workspace) -> SemanticModel:
    """Return the current queryable semantic model without refreshing it."""
    return _require_active_semantic_model(_active_semantic_models(workspace).first())


async def aget_active_semantic_model(workspace) -> SemanticModel:
    """Async counterpart using the same queryability predicate."""
    return _require_active_semantic_model(await _active_semantic_models(workspace).afirst())


async def aworkspace_ids_with_active_semantic_model(workspace_ids) -> set:
    """Of ``workspace_ids``, those :func:`aget_active_semantic_model` would accept."""
    return {
        workspace_id
        async for workspace_id in SemanticModel.objects.filter(
            workspace_id__in=list(workspace_ids), status=SemanticModel.Status.ACTIVE
        )
        .order_by()
        .values_list("workspace_id", flat=True)
    }


def _sync_custom_datasets(
    model: SemanticModel, workspace, schema_name: str
) -> list[dict[str, Any]]:
    """Compile active custom datasets into queryable semantic datasets."""
    diagnostics: list[dict[str, Any]] = []
    valid_custom_ids: set[str] = set()
    physical_datasets = list(
        model.datasets.filter(
            workspace=workspace,
            source_kind=SemanticDataset.SourceKind.PHYSICAL,
            is_visible=True,
        )
    )
    allowed_tables: dict[str, str] = {}
    physical_names = {dataset.name for dataset in physical_datasets}
    for dataset in physical_datasets:
        allowed_tables[dataset.name.lower()] = dataset.table_name
        allowed_tables[dataset.table_name.lower()] = dataset.table_name

    for custom in CustomDataset.objects.filter(
        workspace=workspace,
        is_visible=True,
        status__in=[CustomDataset.Status.ACTIVE, CustomDataset.Status.ERROR],
    ).order_by("name"):
        try:
            if custom.name in physical_names:
                raise CustomDatasetError(
                    f"Custom dataset name '{custom.name}' conflicts with a physical dataset."
                )
            compiled_sql = compile_custom_dataset_sql(
                custom.definition_sql,
                allowed_tables=allowed_tables,
            )
            columns = infer_custom_dataset_columns(workspace, compiled_sql, touch=False)
            if not columns:
                raise CustomDatasetError("Custom dataset query did not return columns.")
        except CustomDatasetError as exc:
            diagnostic = {
                "level": "error",
                "dataset": custom.name,
                "message": str(exc),
            }
            diagnostics.append(diagnostic)
            CustomDataset.objects.filter(id=custom.id).update(
                status=CustomDataset.Status.ERROR,
                diagnostics=[diagnostic],
            )
            SemanticDataset.objects.filter(workspace=workspace, custom_dataset=custom).update(
                is_visible=False
            )
            continue

        existing_custom = (
            SemanticDataset.objects.select_for_update()
            .filter(workspace=workspace, name=custom.name)
            .first()
        )
        defaults = _apply_curation(
            existing_custom,
            {
                "semantic_model": model,
                "label": custom.label or _humanize_name(custom.name),
                "description": custom.description,
                "source_kind": SemanticDataset.SourceKind.CUSTOM,
                "custom_dataset": custom,
                "schema_name": schema_name,
                "table_name": custom.name,
                "row_count": None,
                "is_visible": True,
                "metadata": {
                    "source_type": "custom",
                    "cube_sql": compiled_sql,
                    "row_count_verified": False,
                },
            },
        )
        dataset_writer = _RowWriter(SemanticDataset)
        dataset = dataset_writer.upsert(
            existing_custom, defaults, workspace=workspace, name=custom.name
        )
        dataset_writer.flush()
        _sync_fields(dataset, columns, None)
        # ERROR is a failed validation, not an intentional DRAFT/hidden model.
        # Revalidate after a missing physical dependency becomes available again.
        CustomDataset.objects.filter(id=custom.id).update(
            status=CustomDataset.Status.ACTIVE, diagnostics=[]
        )
        valid_custom_ids.add(str(custom.id))

    stale_custom_datasets = SemanticDataset.objects.filter(
        workspace=workspace,
        source_kind=SemanticDataset.SourceKind.CUSTOM,
    )
    if valid_custom_ids:
        stale_custom_datasets = stale_custom_datasets.exclude(
            custom_dataset_id__in=valid_custom_ids
        )
    stale_custom_datasets.update(is_visible=False)
    return diagnostics


def _relationship_endpoints(rel, datasets_by_table: dict[str, list[SemanticDataset]]):
    """Yield (from_dataset, to_dataset) pairs a pipeline relationship maps onto.

    Matches plain tenant tables by exact name, and multi-tenant namespaced
    views (``<prefix>__<table>``) prefix-for-prefix so tenant A's visits only
    join tenant A's users.
    """
    scoped: dict[tuple[str, str], list[SemanticDataset]] = {}
    legacy: dict[str, list[SemanticDataset]] = {}
    for table_name, datasets in datasets_by_table.items():
        for dataset in datasets:
            metadata = dataset.metadata or {}
            if "source_table_name" in metadata:
                owners = metadata.get("source_tenant_ids", [])
                if len(owners) == 1:
                    scoped.setdefault((owners[0], metadata["source_table_name"]), []).append(
                        dataset
                    )
            else:
                legacy.setdefault(table_name, []).append(dataset)
    for (owner, source_table), from_datasets in scoped.items():
        if source_table == rel.from_table:
            for from_dataset in from_datasets:
                for to_dataset in scoped.get((owner, rel.to_table), []):
                    yield from_dataset, to_dataset
    datasets_by_table = legacy
    for from_dataset in datasets_by_table.get(rel.from_table, []):
        for to_dataset in datasets_by_table.get(rel.to_table, []):
            yield from_dataset, to_dataset
    suffix = f"__{rel.from_table}"
    for table_name, from_datasets in datasets_by_table.items():
        if not table_name.endswith(suffix) or table_name == rel.from_table:
            continue
        prefix = table_name[: -len(suffix)]
        for from_dataset in from_datasets:
            for to_dataset in datasets_by_table.get(view_name(prefix, rel.to_table), []):
                yield from_dataset, to_dataset


def _sync_relationships(model: SemanticModel, workspace) -> list[dict[str, Any]]:
    """Derive dataset relationships from the pipelines' declared table links.

    Pipeline YAMLs declare physical foreign-key-ish links (from_table/from_column
    -> to_table/to_column). Emit one SemanticRelationship per link whose two
    endpoints are visible physical datasets with the referenced columns, with a
    Cube join expression over the generated member names. Only rows stamped
    ``metadata.generated`` are managed here; hand-authored relationships are
    left untouched.
    """
    datasets = list(
        model.datasets.filter(
            workspace=workspace,
            source_kind=SemanticDataset.SourceKind.PHYSICAL,
            is_visible=True,
        ).prefetch_related("fields")
    )
    diagnostics = []
    datasets_by_table: dict[str, list[SemanticDataset]] = {}
    for dataset in datasets:
        metadata = dataset.metadata or {}
        if "source_table_name" in metadata and len(metadata.get("source_tenant_ids", [])) != 1:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "relationship_source_provenance",
                    "message": f"Dataset '{dataset.name}' has ambiguous source ownership; generated joins were omitted.",
                }
            )
        datasets_by_table.setdefault(dataset.table_name, []).append(dataset)

    def visible_field(dataset: SemanticDataset, column: str):
        field_name = semantic_name(column)
        return next(
            (f for f in dataset.fields.all() if f.name == field_name and f.is_visible),
            None,
        )

    active_names: set[str] = set()
    curated_names = {
        name
        for name, metadata in SemanticRelationship.objects.filter(workspace=workspace).values_list(
            "name", "metadata"
        )
        if not (metadata or {}).get("generated")
    }
    # Curated rows are never written here, so only the generated ones are locked.
    relationships_by_name = {
        relationship.name: relationship
        for relationship in SemanticRelationship.objects.select_for_update()
        .filter(workspace=workspace)
        .exclude(name__in=curated_names)
    }
    relationship_writer = _RowWriter(SemanticRelationship)
    for pipeline in get_registry().list():
        for rel in pipeline.relationships:
            for from_dataset, to_dataset in _relationship_endpoints(rel, datasets_by_table):
                pairs = [
                    (visible_field(from_dataset, source), visible_field(to_dataset, target))
                    for source, target in rel.key_pairs
                ]
                if any(source is None or target is None for source, target in pairs):
                    continue
                name = semantic_name(f"{from_dataset.name}_{rel.from_column}_to_{to_dataset.name}")
                if name in curated_names:
                    continue
                if any(
                    not _relationship_key_type(source.data_type)
                    or _relationship_key_type(source.data_type)
                    != _relationship_key_type(target.data_type)
                    or (source.metadata or {}).get("cube_sql")
                    or (target.metadata or {}).get("cube_sql")
                    for source, target in pairs
                ):
                    diagnostics.append(
                        {
                            "level": "warning",
                            "code": "relationship_key_type",
                            "message": f"Relationship '{name}' requires compatible physical scalar keys; arrays/objects require a bridge and calculated keys must be modeled explicitly.",
                        }
                    )
                    continue
                predicates = [
                    f"{{{from_dataset.name}.{source.name}}} = {{{to_dataset.name}.{target.name}}}"
                    for source, target in pairs
                ]
                if rel.require_unique_target:
                    # An incomplete API identity is useful only when it resolves to one row.
                    columns = ", ".join(
                        _quoted_identifier(target.expression) for _, target in pairs
                    )
                    members = ", ".join(
                        f"{{{to_dataset.name}.{target.name}}}" for _, target in pairs
                    )
                    predicates.append(
                        f"({members}) IN (SELECT {columns} FROM {_quoted_identifier(to_dataset.table_name)} "  # noqa: S608
                        f"GROUP BY {columns} HAVING COUNT(*) = 1)"
                    )
                    predicates.extend(
                        f"{{{from_dataset.name}.{source.name}}} <> ''"
                        for source, _ in pairs
                        if _relationship_key_type(source.data_type) == "string"
                    )
                relationships_by_name[name] = relationship_writer.upsert(
                    relationships_by_name.get(name),
                    {
                        "from_dataset": from_dataset,
                        "to_dataset": to_dataset,
                        "relationship_type": rel.relationship_type,
                        "join_expression": " AND ".join(predicates),
                        "metadata": {
                            "generated": True,
                            "description": rel.description,
                            "key_pairs": rel.key_pairs,
                            "key_scope": (
                                "source_tenant"
                                if "source_table_name" in (from_dataset.metadata or {})
                                else "table_name"
                                if from_dataset.table_name == rel.from_table
                                else "view_prefix"
                            ),
                            "require_unique_target": rel.require_unique_target,
                        },
                    },
                    workspace=workspace,
                    name=name,
                )
                active_names.add(name)
    relationship_writer.flush()

    SemanticRelationship.objects.filter(
        workspace=workspace,
        metadata__generated=True,
    ).exclude(name__in=active_names).delete()
    return diagnostics


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _relationship_key_type(data_type: str) -> str | None:
    value = data_type.lower()
    if value in _NUMERIC_TYPES:
        return "number"
    if value in {"text", "character varying", "varchar", "character", "char", "uuid"}:
        return "uuid" if value == "uuid" else "string"
    if value in {"boolean", "bool"}:
        return "boolean"
    if value in {"date", "timestamp without time zone", "timestamp with time zone"}:
        return value
    return None


class _FieldSync:
    """Field writes for one or more datasets, diffed against their stored rows."""

    def __init__(self, datasets) -> None:
        """Load and lock the datasets' fields; callers must be inside a transaction."""
        self._writer = _RowWriter(SemanticField)
        self._rows: dict[Any, dict[str, SemanticField]] = {dataset.pk: {} for dataset in datasets}
        self._hidden: dict[Any, SemanticField] = {}
        if self._rows:
            for field in SemanticField.objects.select_for_update().filter(
                dataset_id__in=list(self._rows)
            ):
                self._rows[field.dataset_id][field.name] = field

    def upsert(self, dataset: SemanticDataset, field_name: str, defaults: dict[str, Any]) -> None:
        rows = self._rows.setdefault(dataset.pk, {})
        existing = rows.get(field_name)
        field = self._writer.upsert(
            existing,
            _apply_curation(existing, defaults),
            dataset=dataset,
            name=field_name,
        )
        rows[field_name] = field
        # A curated is_visible keeps an earlier hide; anything else re-shows it.
        if field.is_visible:
            self._hidden.pop(field.pk, None)

    def hide_stale(self, dataset: SemanticDataset, active_names: set[str]) -> None:
        # Canvas-created fields are user-authored, not column-derived: they never
        # appear in active_names and must not be hidden by the refresh.
        for name, field in self._rows.get(dataset.pk, {}).items():
            if (
                name in active_names
                or not field.is_visible
                or (field.metadata or {}).get("source") == "canvas"
            ):
                continue
            field.is_visible = False
            if not self._writer.include_in_pending_write(field, "is_visible"):
                self._hidden[field.pk] = field

    def flush(self) -> None:
        self._writer.flush()
        if self._hidden:
            # A plain UPDATE, like the stale-field sweep it replaces: hiding a
            # field does not move its updated_at.
            SemanticField.objects.filter(pk__in=list(self._hidden)).update(is_visible=False)
            self._hidden.clear()


def _sync_fields(
    dataset: SemanticDataset,
    columns: list[dict[str, Any]],
    annotation,
    *,
    provider: str = "",
) -> None:
    """Sync one dataset's fields now; its field rows stay locked until the caller commits."""
    with transaction.atomic():
        field_sync = _FieldSync([dataset])
        _derive_fields(field_sync, dataset, columns, annotation, provider=provider)
        field_sync.flush()


def _derive_fields(
    field_sync: _FieldSync,
    dataset: SemanticDataset,
    columns: list[dict[str, Any]],
    annotation,
    *,
    provider: str = "",
) -> None:
    """Queue ``dataset``'s column-derived fields on ``field_sync``."""
    column_notes = annotation.column_notes if annotation else {}
    active_names: set[str] = set()

    def upsert_field(field_name: str, defaults: dict[str, Any]) -> None:
        field_sync.upsert(dataset, field_name, defaults)
        active_names.add(field_name)

    upsert_field(
        "count",
        {
            "label": "Count",
            "description": f"Number of rows in {_humanize_name(dataset.table_name)}.",
            "field_type": SemanticField.FieldType.MEASURE,
            "data_type": "integer",
            "expression": "*",
            "measure_type": SemanticField.MeasureType.COUNT,
            "is_visible": True,
            "metadata": {
                "generated": True,
                **_infer_display_metadata(
                    "count",
                    "integer",
                    measure_type=SemanticField.MeasureType.COUNT,
                ),
            },
        },
    )

    for column in columns:
        column_name = column.get("name", "")
        if not column_name:
            continue
        field_name = semantic_name(column_name)
        data_type = column.get("type") or column.get("data_type") or ""
        description = column_notes.get(column_name) or column.get("description", "")
        event_time = column.get("event_time")
        text_event_time = bool(event_time) and data_type.lower() in {
            "text",
            "character varying",
            "varchar",
        }
        field_type = (
            SemanticField.FieldType.TIME_DIMENSION
            if _is_time(data_type) or text_event_time
            else SemanticField.FieldType.DIMENSION
        )
        # Custom datasets list exactly the columns their author selected.
        hidden_reason = (
            column_hidden_reason(provider, column_name, data_type)
            if dataset.source_kind == SemanticDataset.SourceKind.PHYSICAL
            else ""
        )
        upsert_field(
            field_name,
            {
                "label": _humanize_name(column_name),
                "description": description,
                "field_type": field_type,
                "data_type": "timestamp with time zone" if text_event_time else data_type,
                "expression": column_name,
                "measure_type": "",
                "is_visible": True,
                "metadata": {
                    "source_column": column_name,
                    "nullable": column.get("nullable"),
                    "default": column.get("default"),
                    **({"event_time": event_time} if event_time else {}),
                    **({"cube_sql": event_time_sql(column_name)} if text_event_time else {}),
                    **({HIDDEN_METADATA_KEY: hidden_reason} if hidden_reason else {}),
                    **(
                        {}
                        if _is_identifier_column(column_name, dataset)
                        else _infer_display_metadata(column_name, data_type)
                    ),
                },
            },
        )

        if _is_numeric(data_type) and not _is_identifier_column(column_name, dataset):
            for measure_type, prefix, label_prefix in (
                (SemanticField.MeasureType.SUM, "sum", "Total"),
                (SemanticField.MeasureType.AVG, "avg", "Average"),
            ):
                upsert_field(
                    semantic_name(f"{prefix}_{column_name}"),
                    {
                        "label": f"{label_prefix} {_humanize_name(column_name)}",
                        "description": "",
                        "field_type": SemanticField.FieldType.MEASURE,
                        "data_type": data_type,
                        "expression": column_name,
                        "measure_type": measure_type,
                        "is_visible": True,
                        "metadata": {
                            "source_column": column_name,
                            "generated": True,
                            **_infer_display_metadata(
                                column_name,
                                data_type,
                                measure_type=measure_type,
                            ),
                        },
                    },
                )

    field_sync.hide_stale(dataset, active_names)


def serialize_catalog(model: SemanticModel) -> dict[str, Any]:
    # One relationship fetch for the whole catalog, bucketed by endpoint, so a
    # dataset lists links in BOTH directions (raw_users must show the five
    # datasets pointing at it, not "no relationships").
    outgoing_by_dataset: dict[Any, list[SemanticRelationship]] = {}
    incoming_by_dataset: dict[Any, list[SemanticRelationship]] = {}
    for relationship in SemanticRelationship.objects.filter(
        workspace=model.workspace
    ).select_related("from_dataset", "to_dataset"):
        outgoing_by_dataset.setdefault(relationship.from_dataset_id, []).append(relationship)
        incoming_by_dataset.setdefault(relationship.to_dataset_id, []).append(relationship)
    unpublished = {
        diagnostic.get("relationship")
        for diagnostic in model.diagnostics or []
        if isinstance(diagnostic, dict) and diagnostic.get("code") in DROPPED_JOIN_CODES
    } - {None}

    def relationship_entry(relationship: SemanticRelationship, direction: str) -> dict[str, Any]:
        entry = {**serialize_relationship(relationship), "direction": direction}
        if relationship.name in unpublished:
            # The Cube build dropped this join, so cross-cube queries through it fail.
            entry["published"] = False
        return entry

    datasets = []
    for dataset in (
        model.datasets.filter(is_visible=True)
        .select_related("custom_dataset")
        .prefetch_related("fields")
        .order_by("name")
    ):
        fields = [f for f in dataset.fields.all() if is_listed(f)]
        dimensions = [
            _serialize_field(f) for f in fields if f.field_type == SemanticField.FieldType.DIMENSION
        ]
        time_dimensions = [
            _serialize_field(f)
            for f in fields
            if f.field_type == SemanticField.FieldType.TIME_DIMENSION
        ]
        measures = [
            _serialize_field(f) for f in fields if f.field_type == SemanticField.FieldType.MEASURE
        ]
        relationships = [
            relationship_entry(r, "outgoing") for r in outgoing_by_dataset.get(dataset.id, [])
        ] + [relationship_entry(r, "incoming") for r in incoming_by_dataset.get(dataset.id, [])]
        datasets.append(
            {
                "id": str(dataset.id),
                "name": dataset.name,
                "label": dataset.label or dataset.name,
                "description": dataset.description,
                "source_kind": dataset.source_kind,
                "definition_sql": (
                    dataset.custom_dataset.definition_sql
                    if dataset.source_kind == SemanticDataset.SourceKind.CUSTOM
                    and dataset.custom_dataset_id
                    else ""
                ),
                "schema_name": dataset.schema_name,
                "table_name": dataset.table_name,
                "primary_key": dataset.primary_key,
                "row_count": dataset.row_count,
                "row_count_verified": bool(dataset.metadata.get("row_count_verified")),
                "dimensions": dimensions,
                "time_dimensions": time_dimensions,
                "measures": measures,
                "relationships": relationships,
                "metadata": dataset.metadata,
            }
        )
    return {
        "model": {
            "id": str(model.id),
            "name": model.name,
            "version": model.version,
            "status": model.status,
            "diagnostics": model.diagnostics,
            "last_build": (model.metadata or {}).get("last_build"),
            "updated_at": model.updated_at.isoformat(),
        },
        "datasets": datasets,
    }


def serialize_dataset(dataset: SemanticDataset) -> dict[str, Any]:
    model = dataset.semantic_model
    catalog = serialize_catalog(model)
    for entry in catalog["datasets"]:
        if entry["id"] == str(dataset.id):
            return {"model": catalog["model"], "dataset": entry}
    raise SemanticCatalogUnavailable("Dataset is not visible.")


def _serialize_field(field: SemanticField) -> dict[str, Any]:
    return {
        "id": str(field.id),
        "name": field.name,
        "member": field.member_name,
        "label": field.label or field.name,
        "description": field.description,
        "type": field.field_type,
        "data_type": field.data_type,
        "measure_type": field.measure_type,
        "metadata": field.metadata,
    }


def serialize_relationship(relationship: SemanticRelationship) -> dict[str, Any]:
    return {
        "id": str(relationship.id),
        "name": relationship.name,
        "from_dataset": relationship.from_dataset.name,
        "to_dataset": relationship.to_dataset.name,
        "relationship_type": relationship.relationship_type,
        "join_expression": relationship.join_expression,
    }
