"""Resolve graph source bindings before inspection or runtime validation."""

from apps.semantic.services.date_context import (
    DateContextError,
    comparison_range,
    query_context,
    resolve_date_range,
    resolve_query_dates,
)

from .graph_doc import collect_query_specs, normalize_doc


def resolve_artifact_queries(doc, runtime=None) -> tuple[list[dict], dict]:
    runtime = {} if runtime is None else runtime
    if not isinstance(runtime, dict):
        raise DateContextError("Artifact runtime context must be an object.")
    context = query_context(runtime)
    overrides = runtime.get("sources", {})
    if not isinstance(overrides, dict):
        raise DateContextError("sources must be an object keyed by date-control block id.")
    blocks = {}
    for block in normalize_doc(doc)["blocks"]:
        if (
            not isinstance(block, dict)
            or not isinstance(block.get("id"), str)
            or not isinstance(block.get("type"), str)
            or not isinstance(block.get("config") or {}, dict)
            or not isinstance(block.get("inputs") or {}, dict)
        ):
            raise DateContextError("Invalid artifact block; date bindings cannot be resolved.")
        if block["id"] in blocks:
            raise DateContextError("Duplicate artifact block id; date bindings are ambiguous.")
        blocks[block["id"]] = block
    unknown = set(overrides) - {
        key for key, block in blocks.items() if block["type"] in {"date_filter", "period_selector"}
    }
    if unknown:
        raise DateContextError("Only existing date-control blocks may be overridden.")
    sources = {}
    for key, block in blocks.items():
        config = block.get("config") or {}
        if block["type"] == "date_filter":
            value = overrides.get(key, {"preset": config.get("default", "last_30_days")})
            sources[f"{key}.value"] = resolve_date_range(value, context)
        elif block["type"] == "period_selector":
            current = resolve_date_range(
                overrides.get(key, {"preset": config.get("default_range", "last_30_days")}), context
            )
            previous = comparison_range(
                current, config.get("default_comparison", "previous_period"), context
            )
            sources.update(
                {
                    f"{key}.current": current,
                    f"{key}.previous": previous,
                    f"{key}.pair": {"current": current, "previous": previous},
                }
            )

    def bound(block, port):
        binding = (block.get("inputs") or {}).get(port)
        if binding is None:
            return None
        if not isinstance(binding, dict):
            raise DateContextError(f"Invalid {port} binding.")
        if "$ref" in binding:
            if not isinstance(binding["$ref"], str) or binding["$ref"] not in sources:
                raise DateContextError(f"Unresolved date binding: {binding['$ref']}.")
            return sources[binding["$ref"]]
        if "value" in binding:
            return binding["value"]
        raise DateContextError(f"Invalid {port} binding.")

    queries = []
    for entry in collect_query_specs(doc):
        block = blocks[entry["block_id"]]
        query = dict(entry["query"])
        if (block.get("config") or {}).get("compare"):
            pair = bound(block, "compare")
            if not isinstance(pair, dict) or "current" not in pair or "previous" not in pair:
                raise DateContextError("A comparison query requires current and previous ranges.")
            for suffix, period in (("", "current"), ("_previous", "previous")):
                queries.append(
                    {
                        "name": entry["query_key"] + suffix,
                        **resolve_query_dates({**query, "date_range": pair[period]}, context),
                    }
                )
        else:
            value = bound(block, "date_range")
            if value is not None:
                query["date_range"] = value
            queries.append({"name": entry["query_key"], **resolve_query_dates(query, context)})
    return queries, context
