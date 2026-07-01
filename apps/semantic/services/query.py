"""Structured semantic query execution.

This is intentionally not a general SQL interface. The caller names semantic
members and Scout translates the narrow supported query shape into a Cube query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from asgiref.sync import async_to_sync, sync_to_async
from django.db import close_old_connections

from apps.semantic.models import SemanticDataset, SemanticField
from apps.semantic.services.catalog import SemanticCatalogUnavailable, get_active_semantic_model
from apps.semantic.services.cube_client import CubeClient, CubeConfigurationError
from apps.semantic.services.cube_schema import (
    CubeSchemaBuildError,
    build_cube_security_context,
    get_active_cube_schema,
)
from mcp_server.context import load_workspace_context
from mcp_server.envelope import CONNECTION_ERROR, VALIDATION_ERROR, error_response

MAX_SEMANTIC_LIMIT = 500
SUPPORTED_FILTERS = {
    "equals",
    "notEquals",
    "contains",
    "notContains",
    "gt",
    "gte",
    "lt",
    "lte",
    "inDateRange",
    "set",
    "notSet",
}
SUPPORTED_GRANULARITIES = {"day", "week", "month", "quarter", "year"}


class SemanticQueryError(ValueError):
    pass


@dataclass
class ResolvedMember:
    dataset: SemanticDataset
    field: SemanticField
    member: str

    @property
    def alias(self) -> str:
        return self.member.replace(".", "__")


def run_semantic_query_sync(workspace, query_spec: dict[str, Any]) -> dict[str, Any]:
    return async_to_sync(run_semantic_query)(workspace, query_spec)


async def run_semantic_query(
    workspace,
    query_spec: dict[str, Any],
    *,
    user_id: str = "",
) -> dict[str, Any]:
    """Execute a structured semantic query and return tabular results."""
    try:
        compiled = await sync_to_async(_compile_semantic_query_for_async, thread_sensitive=True)(
            workspace,
            query_spec,
        )
    except SemanticCatalogUnavailable as exc:
        return error_response(VALIDATION_ERROR, str(exc))
    except SemanticQueryError as exc:
        return error_response(VALIDATION_ERROR, str(exc))
    except CubeSchemaBuildError as exc:
        return error_response(VALIDATION_ERROR, str(exc))

    ctx = await load_workspace_context(str(workspace.id))
    security_context = build_cube_security_context(
        workspace,
        compiled["model"],
        compiled["cube_schema"],
        ctx,
        user_id=user_id,
    )
    try:
        result = await CubeClient().execute_query(
            compiled["cube_query"],
            security_context=security_context,
        )
    except CubeConfigurationError as exc:
        return error_response(VALIDATION_ERROR, str(exc))
    except Exception as exc:
        return error_response(CONNECTION_ERROR, f"Cube query execution failed: {exc}")

    return {
        "columns": result.get("columns", []),
        "rows": result.get("rows", []),
        "row_count": result.get("row_count", 0),
        "truncated": compiled["limit"] == MAX_SEMANTIC_LIMIT,
        "semantic_query": compiled["query"],
        "members": compiled["members"],
    }


def _compile_semantic_query_for_async(workspace, query_spec: dict[str, Any]) -> dict[str, Any]:
    try:
        close_old_connections()
        return _compile_semantic_query(workspace, query_spec)
    finally:
        close_old_connections()


def _compile_semantic_query(workspace, query_spec: dict[str, Any]) -> dict[str, Any]:
    model = get_active_semantic_model(workspace)

    measures = _as_list(query_spec.get("measures"))
    dimensions = _as_list(query_spec.get("dimensions"))
    time_dimension = query_spec.get("time_dimension") or query_spec.get("timeDimension") or ""
    granularity = query_spec.get("granularity") or ""
    filters = _as_list(query_spec.get("filters"))
    order_by = _as_list(query_spec.get("order_by") or query_spec.get("orderBy"))
    limit = _coerce_limit(query_spec.get("limit", 100))

    if not measures and not dimensions and not time_dimension:
        raise SemanticQueryError("Provide at least one measure, dimension, or time_dimension.")
    if granularity and granularity not in SUPPORTED_GRANULARITIES:
        raise SemanticQueryError(
            f"Unsupported granularity '{granularity}'. Use one of: {', '.join(sorted(SUPPORTED_GRANULARITIES))}."
        )
    if granularity and not time_dimension:
        raise SemanticQueryError("A granularity requires a time_dimension.")

    resolved_measures = [
        _resolve_member(model, m, expected=SemanticField.FieldType.MEASURE) for m in measures
    ]
    resolved_dimensions = [
        _resolve_member(model, d, expected_any={SemanticField.FieldType.DIMENSION, SemanticField.FieldType.TIME_DIMENSION})
        for d in dimensions
    ]
    resolved_time = (
        _resolve_member(model, time_dimension, expected=SemanticField.FieldType.TIME_DIMENSION)
        if time_dimension
        else None
    )
    resolved_filters = [_resolve_filter(model, f) for f in filters]

    datasets = {
        member.dataset.id
        for member in [*resolved_measures, *resolved_dimensions, *( [resolved_time] if resolved_time else [] )]
        if member is not None
    }
    datasets.update(member.dataset.id for member, _filter in resolved_filters)
    if len(datasets) != 1:
        raise SemanticQueryError(
            "Semantic queries must target one dataset in this first version. Use one dataset's members only."
        )

    members: list[str] = []

    if resolved_time:
        members.append(resolved_time.member)

    for member in resolved_dimensions:
        members.append(member.member)

    for member in resolved_measures:
        members.append(member.member)

    _validate_order_by(order_by, members, resolved_time)
    cube_schema = get_active_cube_schema(workspace, model=model)

    canonical_query = {
        "measures": measures,
        "dimensions": dimensions,
        "time_dimension": time_dimension or None,
        "granularity": granularity or None,
        "filters": filters,
        "order_by": order_by,
        "limit": limit,
    }
    return {
        "cube_query": _cube_query(
            resolved_measures=resolved_measures,
            resolved_dimensions=resolved_dimensions,
            resolved_time=resolved_time,
            granularity=granularity,
            resolved_filters=resolved_filters,
            order_by=order_by,
            limit=limit,
        ),
        "model": model,
        "cube_schema": cube_schema,
        "limit": limit,
        "query": canonical_query,
        "members": members,
    }


def _cube_query(
    *,
    resolved_measures: list[ResolvedMember],
    resolved_dimensions: list[ResolvedMember],
    resolved_time: ResolvedMember | None,
    granularity: str,
    resolved_filters: list[tuple[ResolvedMember, dict[str, Any]]],
    order_by: list,
    limit: int,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "measures": [member.member for member in resolved_measures],
        "dimensions": [member.member for member in resolved_dimensions],
        "filters": [
            _cube_filter(member, filter_spec)
            for member, filter_spec in resolved_filters
        ],
        "limit": limit,
    }
    if resolved_time:
        time_dimension = {"dimension": resolved_time.member}
        if granularity:
            time_dimension["granularity"] = granularity
        query["timeDimensions"] = [time_dimension]
    if order_by:
        query["order"] = _cube_order(order_by)
    elif resolved_time:
        query["order"] = [[resolved_time.member, "asc"]]
    return {key: value for key, value in query.items() if value not in ([], None, "")}


def _cube_filter(member: ResolvedMember, filter_spec: dict[str, Any]) -> dict[str, Any]:
    operator = filter_spec.get("operator", "equals")
    payload: dict[str, Any] = {
        "member": member.member,
        "operator": operator,
    }
    if operator not in {"set", "notSet"}:
        value = filter_spec.get("value")
        if operator == "inDateRange" and (
            not isinstance(value, list | tuple) or len(value) != 2
        ):
            raise SemanticQueryError("inDateRange requires value [start, end].")
        payload["values"] = value if isinstance(value, list) else [value]
    return payload


def _cube_order(order_by: list) -> list[list[str]]:
    order = []
    for item in order_by:
        if not isinstance(item, dict):
            continue
        field = item.get("field") or item.get("member")
        direction = str(item.get("direction", "asc")).lower()
        if direction not in {"asc", "desc"}:
            direction = "asc"
        order.append([str(field), direction])
    return order


def _as_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 100
    return max(1, min(limit, MAX_SEMANTIC_LIMIT))


def _resolve_member(
    model,
    member: str,
    *,
    expected: str | None = None,
    expected_any: set[str] | None = None,
) -> ResolvedMember:
    if not isinstance(member, str) or "." not in member:
        raise SemanticQueryError(f"Invalid semantic member '{member}'. Use dataset.field.")
    dataset_name, field_name = member.split(".", 1)
    dataset = model.datasets.filter(name=dataset_name, is_visible=True).first()
    if dataset is None:
        raise SemanticQueryError(f"Unknown dataset '{dataset_name}'.")
    field = dataset.fields.filter(name=field_name, is_visible=True).first()
    if field is None:
        raise SemanticQueryError(f"Unknown semantic field '{member}'.")
    allowed = expected_any or ({expected} if expected else None)
    if allowed and field.field_type not in allowed:
        allowed_display = ", ".join(sorted(allowed))
        raise SemanticQueryError(f"Member '{member}' must be one of: {allowed_display}.")
    return ResolvedMember(dataset=dataset, field=field, member=member)


def _resolve_filter(model, filter_spec: dict[str, Any]) -> tuple[ResolvedMember, dict[str, Any]]:
    if not isinstance(filter_spec, dict):
        raise SemanticQueryError("Each filter must be an object.")
    field = filter_spec.get("field") or filter_spec.get("member")
    member = _resolve_member(
        model,
        field,
        expected_any={
            SemanticField.FieldType.DIMENSION,
            SemanticField.FieldType.TIME_DIMENSION,
        },
    )
    operator = filter_spec.get("operator", "equals")
    if operator not in SUPPORTED_FILTERS:
        raise SemanticQueryError(f"Unsupported filter operator '{operator}'.")
    return member, filter_spec


def _validate_order_by(
    order_by: list,
    selected_members: list[str],
    resolved_time: ResolvedMember | None,
) -> None:
    selected_aliases = {m.replace(".", "__") for m in selected_members}
    if resolved_time:
        selected_aliases.add("date")
    for item in order_by:
        if not isinstance(item, dict):
            continue
        field = item.get("field") or item.get("member")
        alias = "date" if field == (resolved_time.member if resolved_time else None) else str(field).replace(".", "__")
        if alias not in selected_aliases:
            raise SemanticQueryError(f"Cannot order by '{field}' because it is not selected.")
