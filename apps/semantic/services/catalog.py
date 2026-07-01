"""Semantic catalog bootstrap and serialization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from asgiref.sync import async_to_sync
from django.db import transaction

from apps.knowledge.models import TableKnowledge
from apps.semantic.models import (
    CustomDataset,
    SemanticDataset,
    SemanticField,
    SemanticModel,
    SemanticRelationship,
)
from apps.semantic.services.custom_datasets import (
    CustomDatasetError,
    compile_custom_dataset_sql,
    infer_custom_dataset_columns,
)
from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantMetadata,
    TenantSchema,
    WorkspaceViewSchema,
)
from mcp_server.context import load_workspace_context
from mcp_server.pipeline_registry import get_registry
from mcp_server.services.metadata import (
    pipeline_describe_table,
    pipeline_list_tables,
    pipeline_table_primary_keys,
    workspace_list_tables,
)


class SemanticCatalogUnavailable(Exception):
    """Raised when no queryable schema is available for a workspace."""

    def __init__(self, message: str, schema_status: str = "unavailable") -> None:
        super().__init__(message)
        self.schema_status = schema_status


@dataclass(frozen=True)
class PhysicalTable:
    name: str
    type: str
    description: str
    columns: list[dict[str, Any]]
    materialized_row_count: int | None = None
    materialized_at: str | None = None
    primary_key: str = ""


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


def _is_identifier_column(column_name: str, dataset: SemanticDataset) -> bool:
    """Identifier-ish columns get no sum/avg measures — those aggregates are noise."""
    lowered = column_name.lower()
    return lowered == "id" or lowered.endswith("_id") or column_name == dataset.primary_key


def _is_time(data_type: str) -> bool:
    lowered = data_type.lower()
    return lowered in _TIME_TYPES or "timestamp" in lowered


def _tenant_metadata_for_schema(schema_name: str):
    ts = TenantSchema.objects.filter(schema_name=schema_name).first()
    if ts is None:
        return None
    return TenantMetadata.objects.filter(tenant_membership__tenant_id=ts.tenant_id).first()


def _pipeline_config_for_schema(schema_name: str):
    registry = get_registry()
    ts = TenantSchema.objects.filter(schema_name=schema_name).select_related("tenant").first()
    if ts is None:
        return registry.get("commcare_sync")

    last_run = (
        MaterializationRun.objects.filter(
            tenant_schema=ts,
            state__in=[
                MaterializationRun.RunState.COMPLETED,
                MaterializationRun.RunState.PARTIAL,
            ],
        )
        .order_by("-completed_at")
        .first()
    )
    if last_run:
        cfg = registry.get(last_run.pipeline)
        if cfg:
            return cfg
    return registry.get_by_provider(ts.tenant.provider) or registry.get("commcare_sync")


async def _load_physical_tables_async(workspace) -> tuple[str, list[PhysicalTable]]:
    ctx = await load_workspace_context(str(workspace.id))
    schema_name = ctx.schema_name

    is_view_schema = await WorkspaceViewSchema.objects.filter(
        workspace_id=workspace.id,
        schema_name=schema_name,
        state=SchemaState.ACTIVE,
    ).aexists()

    if is_view_schema:
        table_entries = await workspace_list_tables(ctx)
        pipeline_config = get_registry().get("commcare_sync")
        tenant_metadata = None
    else:
        ts = await TenantSchema.objects.filter(schema_name=schema_name).afirst()
        if ts is None:
            table_entries = await workspace_list_tables(ctx)
            pipeline_config = get_registry().get("commcare_sync")
            tenant_metadata = None
        else:
            last_run = (
                await MaterializationRun.objects.filter(
                    tenant_schema=ts,
                    state__in=[
                        MaterializationRun.RunState.COMPLETED,
                        MaterializationRun.RunState.PARTIAL,
                    ],
                )
                .order_by("-completed_at")
                .afirst()
            )
            registry = get_registry()
            pipeline_config = None
            if last_run:
                pipeline_config = registry.get(last_run.pipeline)
            if pipeline_config is None:
                tenant = await Tenant.objects.aget(id=ts.tenant_id)
                pipeline_config = registry.get_by_provider(tenant.provider)
            if pipeline_config is None:
                pipeline_config = registry.get("commcare_sync")
            table_entries = await pipeline_list_tables(ts, pipeline_config)
            tenant_metadata = await TenantMetadata.objects.filter(
                tenant_membership__tenant_id=ts.tenant_id
            ).afirst()

    primary_keys = await pipeline_table_primary_keys(ctx)
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
        physical_tables.append(
            PhysicalTable(
                name=table_name,
                type=entry.get("type", "table"),
                description=(detail or {}).get("description") or entry.get("description", ""),
                columns=(detail or {}).get("columns", []),
                materialized_row_count=entry.get("materialized_row_count"),
                materialized_at=entry.get("materialized_at"),
                primary_key=primary_keys.get(table_name, ""),
            )
        )
    return schema_name, physical_tables


def load_physical_tables(workspace) -> tuple[str, list[PhysicalTable]]:
    try:
        return async_to_sync(_load_physical_tables_async)(workspace)
    except Exception as exc:
        tenant = workspace.tenant
        schema_status = "unavailable"
        if tenant is not None and TenantSchema.objects.filter(
            tenant=tenant,
            state=SchemaState.PROVISIONING,
        ).exists():
            schema_status = "provisioning"
        raise SemanticCatalogUnavailable(
            "Data unavailable. Please refresh workspace data.",
            schema_status=schema_status,
        ) from exc


def ensure_semantic_model(workspace) -> SemanticModel:
    """Create or refresh the default semantic catalog from active physical tables."""
    schema_name, tables = load_physical_tables(workspace)
    if not tables:
        raise SemanticCatalogUnavailable("No queryable datasets are available.")

    with transaction.atomic():
        model, _ = SemanticModel.objects.select_for_update().get_or_create(
            workspace=workspace,
            defaults={"name": f"{workspace.name} Semantic Model"},
        )
        existing_physical_dataset_ids: set[str] = set()
        for table in tables:
            dataset_name = semantic_name(table.name, fallback="dataset")
            annotation = TableKnowledge.objects.filter(
                workspace=workspace,
                table_name=table.name,
            ).first()
            dataset, _ = SemanticDataset.objects.update_or_create(
                workspace=workspace,
                name=dataset_name,
                defaults={
                    "semantic_model": model,
                    "label": _humanize_name(table.name),
                    "description": (
                        annotation.description if annotation and annotation.description else table.description
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
                    },
                },
            )
            existing_physical_dataset_ids.add(str(dataset.id))
            _sync_fields(dataset, table.columns, annotation)

        SemanticDataset.objects.filter(
            workspace=workspace,
            source_kind=SemanticDataset.SourceKind.PHYSICAL,
        ).exclude(
            id__in=existing_physical_dataset_ids
        ).update(is_visible=False)

        diagnostics = _sync_custom_datasets(model, workspace, schema_name)
        _sync_relationships(model, workspace)
        model.status = SemanticModel.Status.ACTIVE
        model.diagnostics = diagnostics
        model.save(update_fields=["status", "diagnostics", "updated_at"])
        return model


def get_active_semantic_model(workspace) -> SemanticModel:
    """Return the current queryable semantic model without refreshing it."""
    model = SemanticModel.objects.filter(
        workspace=workspace,
        status=SemanticModel.Status.ACTIVE,
    ).first()
    if model is None:
        raise SemanticCatalogUnavailable(
            "No active semantic model is available. Refresh workspace data.",
            schema_status="unavailable",
        )
    return model


def _sync_custom_datasets(model: SemanticModel, workspace, schema_name: str) -> list[dict[str, Any]]:
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
        status=CustomDataset.Status.ACTIVE,
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
            columns = infer_custom_dataset_columns(workspace, compiled_sql)
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

        dataset, _ = SemanticDataset.objects.update_or_create(
            workspace=workspace,
            name=custom.name,
            defaults={
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
        _sync_fields(dataset, columns, None)
        CustomDataset.objects.filter(id=custom.id).update(diagnostics=[])
        valid_custom_ids.add(str(custom.id))

    stale_custom_datasets = SemanticDataset.objects.filter(
        workspace=workspace,
        source_kind=SemanticDataset.SourceKind.CUSTOM,
    )
    if valid_custom_ids:
        stale_custom_datasets = stale_custom_datasets.exclude(custom_dataset_id__in=valid_custom_ids)
    stale_custom_datasets.update(is_visible=False)
    return diagnostics


def _relationship_endpoints(rel, datasets_by_table: dict[str, list[SemanticDataset]]):
    """Yield (from_dataset, to_dataset) pairs a pipeline relationship maps onto.

    Matches plain tenant tables by exact name, and multi-tenant namespaced
    views (``<prefix>__<table>``) prefix-for-prefix so tenant A's visits only
    join tenant A's users.
    """
    for from_dataset in datasets_by_table.get(rel.from_table, []):
        for to_dataset in datasets_by_table.get(rel.to_table, []):
            yield from_dataset, to_dataset
    suffix = f"__{rel.from_table}"
    for table_name, from_datasets in datasets_by_table.items():
        if not table_name.endswith(suffix) or table_name == rel.from_table:
            continue
        prefix = table_name[: -len(suffix)]
        for from_dataset in from_datasets:
            for to_dataset in datasets_by_table.get(f"{prefix}__{rel.to_table}", []):
                yield from_dataset, to_dataset


def _sync_relationships(model: SemanticModel, workspace) -> None:
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
    datasets_by_table: dict[str, list[SemanticDataset]] = {}
    for dataset in datasets:
        datasets_by_table.setdefault(dataset.table_name, []).append(dataset)

    def visible_field(dataset: SemanticDataset, column: str):
        field_name = semantic_name(column)
        return next(
            (f for f in dataset.fields.all() if f.name == field_name and f.is_visible),
            None,
        )

    active_names: set[str] = set()
    for pipeline in get_registry().list():
        for rel in pipeline.relationships:
            for from_dataset, to_dataset in _relationship_endpoints(rel, datasets_by_table):
                from_field = visible_field(from_dataset, rel.from_column)
                to_field = visible_field(to_dataset, rel.to_column)
                if from_field is None or to_field is None:
                    continue
                name = semantic_name(
                    f"{from_dataset.name}_{rel.from_column}_to_{to_dataset.name}"
                )
                relationship_type = (
                    SemanticRelationship.RelationshipType.ONE_TO_ONE
                    if rel.from_column == from_dataset.primary_key
                    else SemanticRelationship.RelationshipType.MANY_TO_ONE
                )
                SemanticRelationship.objects.update_or_create(
                    workspace=workspace,
                    name=name,
                    defaults={
                        "from_dataset": from_dataset,
                        "to_dataset": to_dataset,
                        "relationship_type": relationship_type,
                        "join_expression": (
                            f"{{{from_dataset.name}.{from_field.name}}} = "
                            f"{{{to_dataset.name}.{to_field.name}}}"
                        ),
                        "metadata": {"generated": True, "description": rel.description},
                    },
                )
                active_names.add(name)

    SemanticRelationship.objects.filter(
        workspace=workspace,
        metadata__generated=True,
    ).exclude(name__in=active_names).delete()


def _sync_fields(dataset: SemanticDataset, columns: list[dict[str, Any]], annotation) -> None:
    column_notes = annotation.column_notes if annotation else {}
    active_names: set[str] = set()

    count_field, _ = SemanticField.objects.update_or_create(
        dataset=dataset,
        name="count",
        defaults={
            "label": "Count",
            "description": f"Number of rows in {_humanize_name(dataset.table_name)}.",
            "field_type": SemanticField.FieldType.MEASURE,
            "data_type": "integer",
            "expression": "*",
            "measure_type": SemanticField.MeasureType.COUNT,
            "is_visible": True,
            "metadata": {"generated": True},
        },
    )
    active_names.add(count_field.name)

    for column in columns:
        column_name = column.get("name", "")
        if not column_name:
            continue
        field_name = semantic_name(column_name)
        data_type = column.get("type") or column.get("data_type") or ""
        description = column_notes.get(column_name) or column.get("description", "")
        field_type = (
            SemanticField.FieldType.TIME_DIMENSION
            if _is_time(data_type)
            else SemanticField.FieldType.DIMENSION
        )
        SemanticField.objects.update_or_create(
            dataset=dataset,
            name=field_name,
            defaults={
                "label": _humanize_name(column_name),
                "description": description,
                "field_type": field_type,
                "data_type": data_type,
                "expression": column_name,
                "measure_type": "",
                "is_visible": True,
                "metadata": {
                    "source_column": column_name,
                    "nullable": column.get("nullable"),
                    "default": column.get("default"),
                },
            },
        )
        active_names.add(field_name)

        if _is_numeric(data_type) and not _is_identifier_column(column_name, dataset):
            for measure_type, prefix, label_prefix in (
                (SemanticField.MeasureType.SUM, "sum", "Total"),
                (SemanticField.MeasureType.AVG, "avg", "Average"),
            ):
                measure_name = semantic_name(f"{prefix}_{column_name}")
                SemanticField.objects.update_or_create(
                    dataset=dataset,
                    name=measure_name,
                    defaults={
                        "label": f"{label_prefix} {_humanize_name(column_name)}",
                        "description": "",
                        "field_type": SemanticField.FieldType.MEASURE,
                        "data_type": data_type,
                        "expression": column_name,
                        "measure_type": measure_type,
                        "is_visible": True,
                        "metadata": {"source_column": column_name, "generated": True},
                    },
                )
                active_names.add(measure_name)

    SemanticField.objects.filter(dataset=dataset).exclude(name__in=active_names).update(
        is_visible=False
    )


def serialize_catalog(model: SemanticModel) -> dict[str, Any]:
    datasets = []
    for dataset in (
        model.datasets.filter(is_visible=True)
        .prefetch_related("fields")
        .order_by("name")
    ):
        fields = [f for f in dataset.fields.all() if f.is_visible]
        dimensions = [
            _serialize_field(f)
            for f in fields
            if f.field_type == SemanticField.FieldType.DIMENSION
        ]
        time_dimensions = [
            _serialize_field(f)
            for f in fields
            if f.field_type == SemanticField.FieldType.TIME_DIMENSION
        ]
        measures = [
            _serialize_field(f)
            for f in fields
            if f.field_type == SemanticField.FieldType.MEASURE
        ]
        relationships = [
            serialize_relationship(r)
            for r in SemanticRelationship.objects.filter(
                workspace=model.workspace,
                from_dataset=dataset,
            ).select_related("to_dataset")
        ]
        datasets.append(
            {
                "id": str(dataset.id),
                "name": dataset.name,
                "label": dataset.label or dataset.name,
                "description": dataset.description,
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
