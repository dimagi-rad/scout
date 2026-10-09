"""Structured semantic query execution.

This is intentionally not a general SQL interface. The caller names semantic
members and Scout translates the narrow supported query shape into a Cube query.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from asgiref.sync import async_to_sync, sync_to_async
from django.db import close_old_connections

from apps.common.capacity import CapacityExhausted, CapacityResource, report_capacity_exhausted
from apps.common.errors import validation_error_code
from apps.semantic.models import SemanticDataset, SemanticField
from apps.semantic.services.catalog import SemanticCatalogUnavailable, get_active_semantic_model
from apps.semantic.services.cube_client import (
    CubeAuthenticationError,
    CubeClient,
    CubeConfigurationError,
    CubeConnectionError,
    CubeQueryError,
)
from apps.semantic.services.cube_schema import (
    CubeSchemaBuildError,
    build_cube_security_context,
    get_active_cube_schema,
)
from apps.semantic.services.date_context import (
    DateContextError,
    resolve_query_dates,
    validate_date_filter,
)
from apps.semantic.services.query_outcomes import QueryReadiness, query_error, query_readiness_error
from mcp_server.context import load_workspace_context
from mcp_server.envelope import CONNECTION_ERROR, VALIDATION_ERROR

MAX_SEMANTIC_LIMIT = 500
CAPACITY_EXHAUSTED_CATEGORY = "capacity_exhausted"
SUPPORTED_GRANULARITIES = {"day", "week", "month", "quarter", "year"}


class SemanticQueryError(ValueError):
    pass


class SemanticMemberError(SemanticQueryError):
    """A query's required member is missing, hidden, or changed type."""


@dataclass
class ResolvedMember:
    dataset: SemanticDataset
    field: SemanticField
    member: str

    @property
    def alias(self) -> str:
        return self.member.replace(".", "__")


def is_capacity_exhausted(result: dict[str, Any]) -> bool:
    error = result.get("error")
    return isinstance(error, dict) and error.get("category") == CAPACITY_EXHAUSTED_CATEGORY


def raise_if_capacity_exhausted(result: dict[str, Any]) -> None:
    """Let an HTTP caller answer a full Cube pool with the shared "busy" 503."""
    if is_capacity_exhausted(result):
        raise CapacityExhausted(CapacityResource.CUBE, result["error"].get("message", ""))


def run_semantic_query_sync(workspace, query_spec: dict[str, Any]) -> dict[str, Any]:
    return async_to_sync(run_semantic_query)(workspace, query_spec)


async def run_semantic_query(
    workspace,
    query_spec: dict[str, Any],
    *,
    user_id: str = "",
    readiness: QueryReadiness | None = None,
    max_limit: int = MAX_SEMANTIC_LIMIT,
) -> dict[str, Any]:
    """Execute a structured semantic query and return tabular results.

    ``max_limit`` caps the row limit; only the authorized data export raises it
    above the agent-facing ``MAX_SEMANTIC_LIMIT``.
    """
    try:
        compiled = await sync_to_async(_compile_semantic_query_for_async, thread_sensitive=True)(
            workspace,
            query_spec,
            max_limit,
        )
    except SemanticCatalogUnavailable as exc:
        return await query_readiness_error(
            workspace,
            query_spec,
            exc.code,
            str(exc),
            category="data_unavailable",
            readiness=readiness,
        )
    except SemanticMemberError as exc:
        return query_error(VALIDATION_ERROR, str(exc), category="missing_model_dependency")
    except (SemanticQueryError, DateContextError) as exc:
        return query_error(VALIDATION_ERROR, str(exc), category="invalid_query")
    except CubeSchemaBuildError as exc:
        return await query_readiness_error(
            workspace,
            query_spec,
            exc.code,
            str(exc),
            category="data_unavailable",
            readiness=readiness,
        )

    try:
        ctx = await load_workspace_context(str(workspace.id))
    except ValueError as exc:
        # An expired/teardown tenant schema is an expected validation state,
        # not an agent-stream failure.  Keep it inside the semantic tool's
        # structured error envelope so the agent can explain that the data
        # needs to be materialized again.
        return await query_readiness_error(
            workspace,
            query_spec,
            validation_error_code(exc),
            str(exc),
            category="data_unavailable",
            readiness=readiness,
        )
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
    except (CubeConfigurationError, CubeAuthenticationError) as exc:
        return query_error(VALIDATION_ERROR, str(exc), category="configuration_required")
    except CubeConnectionError as exc:
        if exc.capacity_resource is not None:
            await sync_to_async(report_capacity_exhausted)(exc.capacity_resource, str(exc))
            return query_error(
                CONNECTION_ERROR,
                "Cube is at its database connection limit. Retry the query in a few seconds.",
                category=CAPACITY_EXHAUSTED_CATEGORY,
                retryable=True,
            )
        return query_error(
            CONNECTION_ERROR,
            f"Cube query execution failed: {exc}",
            category="transient_runtime_failure",
            retryable=True,
        )
    except CubeQueryError as exc:
        # Compilation used the semantic catalog; Cube may be serving an older
        # publication. Readiness, not the error's wording, decides whether that
        # mismatch warrants rebuilding. A ready surface remains invalid_query.
        return await query_readiness_error(
            workspace,
            query_spec,
            VALIDATION_ERROR,
            f"Cube query execution failed: {exc}",
            category="invalid_query",
            readiness=readiness,
        )
    except Exception as exc:
        return query_error(
            CONNECTION_ERROR, f"Cube query execution failed: {exc}", category="runtime_failure"
        )

    return {
        "columns": result.get("columns", []),
        "rows": result.get("rows", []),
        "row_count": result.get("row_count", 0),
        # A full page means the limit may have cut the result set off.
        "truncated": result.get("row_count", 0) >= compiled["limit"],
        "semantic_query": compiled["query"],
        "members": compiled["members"],
        "field_metadata": compiled["field_metadata"],
    }


def _compile_semantic_query_for_async(
    workspace, query_spec: dict[str, Any], max_limit: int = MAX_SEMANTIC_LIMIT
) -> dict[str, Any]:
    try:
        close_old_connections()
        return _compile_semantic_query(workspace, query_spec, max_limit=max_limit)
    finally:
        close_old_connections()


def _compile_semantic_query(
    workspace, query_spec: dict[str, Any], *, max_limit: int = MAX_SEMANTIC_LIMIT
) -> dict[str, Any]:
    query_spec = resolve_query_dates(query_spec)
    model = get_active_semantic_model(workspace)

    measures = _as_list(query_spec.get("measures"))
    dimensions = _as_list(query_spec.get("dimensions"))
    time_dimension = query_spec.get("time_dimension") or query_spec.get("timeDimension") or ""
    granularity = query_spec.get("granularity") or ""
    filters = _as_list(query_spec.get("filters"))
    order_by = _as_list(query_spec.get("order_by") or query_spec.get("orderBy"))
    limit = _coerce_limit(query_spec.get("limit", 100), max_limit)

    if not measures and not dimensions and not time_dimension:
        raise SemanticQueryError("Provide at least one measure, dimension, or time_dimension.")
    if granularity and (
        not isinstance(granularity, str) or granularity not in SUPPORTED_GRANULARITIES
    ):
        raise SemanticQueryError(
            f"Unsupported granularity '{granularity}'. Use one of: {', '.join(sorted(SUPPORTED_GRANULARITIES))}."
        )
    if granularity and not time_dimension:
        raise SemanticQueryError("A granularity requires a time_dimension.")

    resolver = _MemberResolver(
        model,
        [
            *measures,
            *dimensions,
            time_dimension,
            *(f.get("field") or f.get("member") for f in filters if isinstance(f, dict)),
        ],
    )
    resolved_measures = [
        resolver.resolve(m, expected=SemanticField.FieldType.MEASURE) for m in measures
    ]
    resolved_dimensions = [
        resolver.resolve(
            d,
            expected_any={
                SemanticField.FieldType.DIMENSION,
                SemanticField.FieldType.TIME_DIMENSION,
            },
        )
        for d in dimensions
    ]
    resolved_time = (
        resolver.resolve(time_dimension, expected=SemanticField.FieldType.TIME_DIMENSION)
        if time_dimension
        else None
    )
    resolved_filters = [
        _resolve_filter(
            resolver, f, timezone_name=(query_spec.get("query_context") or {}).get("timezone")
        )
        for f in filters
    ]

    datasets = {
        member.dataset.id
        for member in [
            *resolved_measures,
            *resolved_dimensions,
            *([resolved_time] if resolved_time else []),
        ]
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
    if query_spec.get("query_context"):
        canonical_query["query_context"] = query_spec["query_context"]
    return {
        "cube_query": _cube_query(
            resolved_measures=resolved_measures,
            resolved_dimensions=resolved_dimensions,
            resolved_time=resolved_time,
            granularity=granularity,
            resolved_filters=resolved_filters,
            order_by=order_by,
            limit=limit,
            timezone_name=(query_spec.get("query_context") or {}).get("timezone", ""),
        ),
        "model": model,
        "cube_schema": cube_schema,
        "limit": limit,
        "query": canonical_query,
        "members": members,
        "field_metadata": {
            member.member: {
                "field_type": member.field.field_type,
                "data_type": member.field.data_type,
                **({"granularity": granularity} if member is resolved_time else {}),
            }
            for member in [
                *resolved_measures,
                *resolved_dimensions,
                *([resolved_time] if resolved_time else []),
            ]
        },
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
    timezone_name: str = "",
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "measures": [member.member for member in resolved_measures],
        "dimensions": [member.member for member in resolved_dimensions],
        "filters": [_cube_filter(member, filter_spec) for member, filter_spec in resolved_filters],
        "limit": limit,
    }
    if timezone_name:
        query["timezone"] = timezone_name
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
        value = filter_spec.get("value") if "value" in filter_spec else filter_spec.get("values")
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


def _coerce_limit(value: Any, max_limit: int = MAX_SEMANTIC_LIMIT) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 100
    return max(1, min(limit, max_limit))


class _MemberResolver:
    """Resolves ``dataset.field`` members for one query from a single batched lookup.

    Per-member lookups cost two queries each, which Sentry flagged as an N+1 on
    /semantic-query/ (SCOUT-DJANGO-3X).
    """

    def __init__(self, model, members: list) -> None:
        pairs = [m.split(".", 1) for m in members if isinstance(m, str) and "." in m]
        self._datasets = {
            dataset.name: dataset
            for dataset in model.datasets.filter(
                is_visible=True, name__in={dataset_name for dataset_name, _ in pairs}
            )
        }
        datasets_by_id = {dataset.id: dataset for dataset in self._datasets.values()}
        self._fields: dict[tuple[uuid.UUID, str], SemanticField] = {}
        for field in SemanticField.objects.filter(
            dataset_id__in=list(datasets_by_id),
            is_visible=True,
            name__in={field_name for _, field_name in pairs},
        ):
            field.dataset = datasets_by_id[field.dataset_id]
            self._fields[(field.dataset_id, field.name)] = field

    def resolve(
        self,
        member: str,
        *,
        expected: str | None = None,
        expected_any: set[str] | None = None,
    ) -> ResolvedMember:
        if not isinstance(member, str) or "." not in member:
            raise SemanticQueryError(f"Invalid semantic member '{member}'. Use dataset.field.")
        dataset_name, field_name = member.split(".", 1)
        dataset = self._datasets.get(dataset_name)
        if dataset is None:
            raise SemanticMemberError(f"Unknown dataset '{dataset_name}'.")
        field = self._fields.get((dataset.id, field_name))
        if field is None:
            raise SemanticMemberError(f"Unknown semantic field '{member}'.")
        allowed = expected_any or ({expected} if expected else None)
        if allowed and field.field_type not in allowed:
            allowed_display = ", ".join(sorted(allowed))
            raise SemanticMemberError(f"Member '{member}' must be one of: {allowed_display}.")
        return ResolvedMember(dataset=dataset, field=field, member=member)


def _resolve_filter(
    resolver: _MemberResolver, filter_spec: dict[str, Any], *, timezone_name=None
) -> tuple[ResolvedMember, dict[str, Any]]:
    if not isinstance(filter_spec, dict):
        raise SemanticQueryError("Each filter must be an object.")
    validate_date_filter(filter_spec, timezone_name)
    field = filter_spec.get("field") or filter_spec.get("member")
    member = resolver.resolve(
        field,
        expected_any={
            SemanticField.FieldType.DIMENSION,
            SemanticField.FieldType.TIME_DIMENSION,
        },
    )
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
        alias = (
            "date"
            if field == (resolved_time.member if resolved_time else None)
            else str(field).replace(".", "__")
        )
        if alias not in selected_aliases:
            raise SemanticQueryError(f"Cannot order by '{field}' because it is not selected.")
