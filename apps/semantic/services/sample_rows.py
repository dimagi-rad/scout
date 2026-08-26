"""Bounded semantic samples for dataset inspection."""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

from apps.semantic.models import SemanticDataset, SemanticField
from apps.semantic.services.catalog import SemanticCatalogUnavailable, get_active_semantic_model
from apps.semantic.services.query import run_semantic_query_sync

MAX_SAMPLE_ROWS = 20
MAX_SAMPLE_FIELDS = 16


def sample_dataset_rows(
    workspace,
    dataset_ref: str,
    limit: int = 5,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Return a bounded sample via the saved semantic model."""
    model = get_active_semantic_model(workspace)
    dataset = _resolve_dataset(model, dataset_ref)
    bounded_limit = max(1, min(int(limit or 5), MAX_SAMPLE_ROWS))
    selected = _selected_fields(dataset, fields)
    dimensions = [
        field.member_name
        for field in selected
        if field.field_type
        in {SemanticField.FieldType.DIMENSION, SemanticField.FieldType.TIME_DIMENSION}
    ]
    measures = [
        field.member_name for field in selected if field.field_type == SemanticField.FieldType.MEASURE
    ]
    query_spec = {
        "dimensions": dimensions,
        "measures": measures,
        "limit": bounded_limit,
    }
    result = run_semantic_query_sync(workspace, query_spec)
    if result.get("success") is False:
        error = result.get("error") or {}
        raise SemanticCatalogUnavailable(
            f"Semantic sample query failed: {error.get('message') or 'unknown error'}"
        )

    return {
        "dataset": dataset.name,
        "columns": result.get("columns", []),
        "rows": _row_objects(result.get("columns", []), result.get("rows", [])),
        "row_count": result.get("row_count", 0),
        "limit": bounded_limit,
        "members": result.get("members", [*dimensions, *measures]),
        "semantic_query": result.get("semantic_query", query_spec),
        "sample_kind": "semantic_query",
    }


def _resolve_dataset(model, dataset_ref: str) -> SemanticDataset:
    ref = str(dataset_ref or "").strip()
    if not ref:
        raise SemanticCatalogUnavailable("dataset is required.")
    dataset = model.datasets.filter(is_visible=True, name=ref).first()
    if dataset is None:
        with_uuid = None
        with contextlib.suppress(ValueError):
            with_uuid = UUID(ref)
        if with_uuid is not None:
            dataset = model.datasets.filter(is_visible=True, id=with_uuid).first()
    if dataset is None:
        raise SemanticCatalogUnavailable(f"Unknown dataset '{dataset_ref}'.")
    return dataset


def _selected_fields(dataset: SemanticDataset, field_refs: list[str] | None) -> list[SemanticField]:
    visible = list(dataset.fields.filter(is_visible=True).order_by("field_type", "name"))
    if field_refs:
        selected = [_resolve_field_ref(dataset, visible, ref) for ref in field_refs]
    else:
        selected = [
            field
            for field in visible
            if field.field_type
            in {SemanticField.FieldType.DIMENSION, SemanticField.FieldType.TIME_DIMENSION}
        ]
        if not selected:
            selected = [
                field
                for field in visible
                if field.field_type == SemanticField.FieldType.MEASURE
            ][:1]
    selected = selected[:MAX_SAMPLE_FIELDS]
    if not selected:
        raise SemanticCatalogUnavailable(f"Dataset '{dataset.name}' has no visible fields to sample.")
    return selected


def _resolve_field_ref(
    dataset: SemanticDataset,
    visible_fields: list[SemanticField],
    ref: str,
) -> SemanticField:
    raw_ref = str(ref or "").strip()
    dataset_name = dataset.name
    field_name = raw_ref
    if "." in raw_ref:
        dataset_name, field_name = raw_ref.split(".", 1)
    if dataset_name != dataset.name:
        raise SemanticCatalogUnavailable(
            f"Sample field '{raw_ref}' does not belong to dataset '{dataset.name}'."
        )
    field = next((field for field in visible_fields if field.name == field_name), None)
    if field is None:
        raise SemanticCatalogUnavailable(f"Unknown visible field '{raw_ref}'.")
    return field


def _row_objects(columns: list[str], rows: list) -> list[dict[str, Any]]:
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows
    return [
        {column: value for column, value in zip(columns, row, strict=True)}
        for row in rows
    ]
