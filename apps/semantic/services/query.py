"""Structured semantic query execution.

This is intentionally not a general SQL interface. The caller names semantic
members and Scout compiles the narrow supported query shape into trusted SQL.
The backend can be swapped to Cube without changing the API contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from asgiref.sync import async_to_sync, sync_to_async

from apps.semantic.models import SemanticDataset, SemanticField
from apps.semantic.services.catalog import SemanticCatalogUnavailable, ensure_semantic_model
from mcp_server.context import load_workspace_context
from mcp_server.envelope import VALIDATION_ERROR, error_response
from mcp_server.services.query import execute_internal_query

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


async def run_semantic_query(workspace, query_spec: dict[str, Any]) -> dict[str, Any]:
    """Execute a structured semantic query and return tabular results."""
    try:
        compiled = await sync_to_async(_compile_semantic_query, thread_sensitive=True)(
            workspace,
            query_spec,
        )
    except SemanticCatalogUnavailable as exc:
        return error_response(VALIDATION_ERROR, str(exc))
    except SemanticQueryError as exc:
        return error_response(VALIDATION_ERROR, str(exc))

    ctx = await load_workspace_context(str(workspace.id))
    result = await execute_internal_query(ctx, compiled["sql"], compiled["params"])
    if not result.get("success", True):
        return result

    return {
        "columns": result.get("columns", []),
        "rows": result.get("rows", []),
        "row_count": result.get("row_count", 0),
        "truncated": compiled["limit"] == MAX_SEMANTIC_LIMIT,
        "semantic_query": compiled["query"],
        "members": compiled["members"],
    }


def _compile_semantic_query(workspace, query_spec: dict[str, Any]) -> dict[str, Any]:
    model = ensure_semantic_model(workspace)

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

    dataset = (
        resolved_measures[0].dataset
        if resolved_measures
        else resolved_dimensions[0].dataset
        if resolved_dimensions
        else resolved_time.dataset
        if resolved_time
        else resolved_filters[0][0].dataset
    )

    select_parts: list[str] = []
    group_parts: list[str] = []
    params: list[Any] = []
    members: list[str] = []

    if resolved_time:
        time_expr = _quoted_column(resolved_time.field.expression)
        alias = "date" if granularity else resolved_time.alias
        if granularity:
            # Granularity is validated against SUPPORTED_GRANULARITIES above.
            # Keep it literal so repeated SELECT/GROUP BY expressions do not
            # require duplicated bound parameters.
            expr = f"date_trunc('{granularity}', {time_expr})::date"
        else:
            expr = time_expr
        select_parts.append(f"{expr} AS {_quoted_identifier(alias)}")
        group_parts.append(expr)
        members.append(resolved_time.member)

    for member in resolved_dimensions:
        expr = _quoted_column(member.field.expression)
        select_parts.append(f"{expr} AS {_quoted_identifier(member.alias)}")
        group_parts.append(expr)
        members.append(member.member)

    for member in resolved_measures:
        select_parts.append(f"{_measure_sql(member.field)} AS {_quoted_identifier(member.alias)}")
        members.append(member.member)

    where_parts: list[str] = []
    for member, filter_spec in resolved_filters:
        where_parts.append(_filter_sql(member, filter_spec, params))

    if not select_parts:
        select_parts.append("COUNT(*) AS count")

    sql_parts = [
        f"SELECT {', '.join(select_parts)}",
        f"FROM {_quoted_identifier(dataset.table_name)}",
    ]
    if where_parts:
        sql_parts.append(f"WHERE {' AND '.join(where_parts)}")
    if group_parts:
        sql_parts.append(f"GROUP BY {', '.join(group_parts)}")

    order_parts = _order_by_sql(order_by, members, resolved_time)
    if order_parts:
        sql_parts.append(f"ORDER BY {', '.join(order_parts)}")
    elif resolved_time:
        sql_parts.append(f"ORDER BY {_quoted_identifier('date' if granularity else resolved_time.alias)} ASC")

    sql_parts.append(f"LIMIT {limit}")

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
        "sql": "\n".join(sql_parts),
        "params": tuple(params),
        "limit": limit,
        "query": canonical_query,
        "members": members,
    }


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


def _measure_sql(field: SemanticField) -> str:
    measure_type = field.measure_type
    if measure_type == SemanticField.MeasureType.COUNT:
        return "COUNT(*)"
    expr = _quoted_column(field.expression)
    if measure_type == SemanticField.MeasureType.SUM:
        return f"SUM({expr})"
    if measure_type == SemanticField.MeasureType.AVG:
        return f"AVG({expr})"
    if measure_type == SemanticField.MeasureType.MIN:
        return f"MIN({expr})"
    if measure_type == SemanticField.MeasureType.MAX:
        return f"MAX({expr})"
    if measure_type == SemanticField.MeasureType.NUMBER:
        return expr
    raise SemanticQueryError(f"Unsupported measure type '{measure_type}' for {field.member_name}.")


def _filter_sql(member: ResolvedMember, filter_spec: dict[str, Any], params: list[Any]) -> str:
    operator = filter_spec.get("operator", "equals")
    value = filter_spec.get("value")
    expr = _measure_sql(member.field) if member.field.field_type == SemanticField.FieldType.MEASURE else _quoted_column(member.field.expression)

    if operator == "equals":
        params.append(value)
        return f"{expr} = %s"
    if operator == "notEquals":
        params.append(value)
        return f"{expr} <> %s"
    if operator == "contains":
        params.append(f"%{value}%")
        return f"{expr}::text ILIKE %s"
    if operator == "notContains":
        params.append(f"%{value}%")
        return f"{expr}::text NOT ILIKE %s"
    if operator == "gt":
        params.append(value)
        return f"{expr} > %s"
    if operator == "gte":
        params.append(value)
        return f"{expr} >= %s"
    if operator == "lt":
        params.append(value)
        return f"{expr} < %s"
    if operator == "lte":
        params.append(value)
        return f"{expr} <= %s"
    if operator == "inDateRange":
        if not isinstance(value, list | tuple) or len(value) != 2:
            raise SemanticQueryError("inDateRange requires value [start, end].")
        params.extend([value[0], value[1]])
        return f"{expr} BETWEEN %s AND %s"
    if operator == "set":
        return f"{expr} IS NOT NULL"
    if operator == "notSet":
        return f"{expr} IS NULL"
    raise SemanticQueryError(f"Unsupported filter operator '{operator}'.")


def _order_by_sql(order_by: list, selected_members: list[str], resolved_time: ResolvedMember | None) -> list[str]:
    parts = []
    selected_aliases = {m.replace(".", "__") for m in selected_members}
    if resolved_time:
        selected_aliases.add("date")
    for item in order_by:
        if not isinstance(item, dict):
            continue
        field = item.get("field") or item.get("member")
        direction = str(item.get("direction", "asc")).lower()
        if direction not in {"asc", "desc"}:
            direction = "asc"
        alias = "date" if field == (resolved_time.member if resolved_time else None) else str(field).replace(".", "__")
        if alias not in selected_aliases:
            raise SemanticQueryError(f"Cannot order by '{field}' because it is not selected.")
        parts.append(f"{_quoted_identifier(alias)} {direction.upper()}")
    return parts


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quoted_column(value: str) -> str:
    if value == "*":
        return value
    if "." in value or '"' in value:
        raise SemanticQueryError(f"Unsupported field expression '{value}'.")
    return _quoted_identifier(value)
