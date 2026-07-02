"""Pure helpers for Scout graph artifact documents.

Graph artifacts are declarative story documents stored in
``Artifact.data["story_doc"]``. These helpers intentionally avoid database
access so validation, atomic apply, manifest extraction, and frontend parity
tests can share one document contract.
"""

from __future__ import annotations

import copy
import re
from typing import Any

CURRENT_SCHEMA_VERSION = 1

KNOWN_BLOCK_TYPES = {
    "title",
    "section",
    "question",
    "tldr",
    "markdown",
    "date_filter",
    "period_selector",
    "semantic_query",
    "graph",
    "table",
    "stat",
}

ALLOWED_QUERY_KEYS = {
    "measures",
    "dimensions",
    "time_dimension",
    "granularity",
    "filters",
    "order_by",
    "limit",
}

RAW_QUERY_KEYS = {
    "sql",
    "source_queries",
    "query",
    "timeDimensions",
    "dateRange",
    "order",
    "segments",
    "timezone",
}

ALLOWED_GRANULARITIES = {"day", "week", "month", "quarter", "year"}

CONFIG_KEYS = {
    "title": {"text", "subtitle"},
    "section": {"title", "body"},
    "question": {"text"},
    "tldr": {"content", "items"},
    "markdown": {"body", "content"},
    "date_filter": {"label", "default"},
    "period_selector": {"label", "default_range", "default_comparison"},
    "semantic_query": {"queries", "compare"},
    "graph": {
        "title",
        "chart_type",
        "x_key",
        "y_key",
        "series",
        "data_label",
        "recharts",
        "query",
        "transform",
        "stacked",
        "y_format",
        "height",
    },
    "table": {"title", "columns", "query"},
    "stat": {"title", "label", "value_path", "value_key", "format", "delta_path"},
}

RECHARTS_COMPONENT_TYPES = {
    "Area",
    "AreaChart",
    "Bar",
    "BarChart",
    "CartesianGrid",
    "Cell",
    "ComposedChart",
    "Legend",
    "Line",
    "LineChart",
    "Pie",
    "PieChart",
    "ReferenceLine",
    "Scatter",
    "ScatterChart",
    "Tooltip",
    "XAxis",
    "YAxis",
}

RECHARTS_DATA_TYPES = {
    "AreaChart",
    "BarChart",
    "ComposedChart",
    "LineChart",
    "Pie",
    "PieChart",
    "ScatterChart",
}

RECHARTS_RESULT_KEY_PROPS = {"dataKey", "nameKey", "xAxisKey", "yAxisKey"}


class GraphDocError(ValueError):
    """Raised when an atomic graph doc edit is invalid."""


def problem(
    message: str,
    *,
    block_id: str | None = None,
    code: str | None = None,
    severity: str = "error",
) -> dict[str, Any]:
    item: dict[str, Any] = {"severity": severity, "message": message}
    if block_id:
        item["block_id"] = block_id
    if code:
        item["code"] = code
    return item


def story_doc_from_artifact_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"schema_version": CURRENT_SCHEMA_VERSION, "blocks": []}
    story_doc = data.get("story_doc")
    if isinstance(story_doc, dict):
        return story_doc
    return {"schema_version": CURRENT_SCHEMA_VERSION, "blocks": []}


def normalize_doc(doc: Any, *, name: str = "") -> dict[str, Any]:
    """Return a v1-shaped doc while preserving existing block payloads."""
    if not isinstance(doc, dict):
        doc = {}
    normalized = copy.deepcopy(doc)
    if "schema_version" not in normalized:
        normalized["schema_version"] = normalized.get("version", CURRENT_SCHEMA_VERSION)
    normalized.pop("version", None)
    if name and not normalized.get("name"):
        normalized["name"] = name
    if "blocks" not in normalized or not isinstance(normalized.get("blocks"), list):
        normalized["blocks"] = []
    return normalized


def validate_doc(doc: Any) -> list[dict[str, Any]]:
    doc = normalize_doc(doc)
    diagnostics: list[dict[str, Any]] = []
    if doc.get("schema_version") != CURRENT_SCHEMA_VERSION:
        diagnostics.append(
            problem(
                f"Unsupported graph schema_version {doc.get('schema_version')!r}",
                code="schema_version",
            )
        )

    blocks = doc.get("blocks")
    if not isinstance(blocks, list):
        return [problem("Graph doc must contain a blocks array", code="doc_shape")]

    block_map: dict[str, dict[str, Any]] = {}
    outputs: dict[str, dict[str, str]] = {}
    refs_by_block: dict[str, list[str]] = {}

    for block in blocks:
        if not isinstance(block, dict):
            diagnostics.append(problem("Every block must be an object", code="block_shape"))
            continue
        block_id = block.get("id")
        block_type = block.get("type")
        if not isinstance(block_id, str) or not block_id:
            diagnostics.append(problem("Every block needs a non-empty string id", code="block_id"))
            continue
        if block_id in block_map:
            diagnostics.append(
                problem(f'Duplicate block id "{block_id}"', block_id=block_id, code="duplicate_id")
            )
            continue
        if block_type not in KNOWN_BLOCK_TYPES:
            diagnostics.append(
                problem(
                    f'Unknown block type "{block_type}"',
                    block_id=block_id,
                    code="unknown_block_type",
                )
            )
            continue
        config = block.get("config")
        if config is None:
            block["config"] = {}
            config = block["config"]
        if not isinstance(config, dict):
            diagnostics.append(
                problem("Block config must be an object", block_id=block_id, code="config_shape")
            )
            continue
        allowed = CONFIG_KEYS.get(block_type, set())
        unknown_keys = sorted(k for k in config if k not in allowed)
        if unknown_keys:
            allowed_text = ", ".join(sorted(allowed)) if allowed else "(none)"
            diagnostics.append(
                problem(
                    "Unsupported config key(s): "
                    f"{', '.join(unknown_keys)}. "
                    f"Allowed config keys for {block_type}: {allowed_text}",
                    block_id=block_id,
                    code="unknown_config_key",
                )
            )
        block_map[block_id] = block
        outputs[block_id] = block_output_ports(block)
        diagnostics.extend(_validate_block_config(block))

    for block_id, block in block_map.items():
        block_diags, refs = _validate_bindings(block, outputs)
        diagnostics.extend(block_diags)
        refs_by_block[block_id] = refs

    diagnostics.extend(_cycle_diagnostics(refs_by_block))
    diagnostics.extend(_key_contract_warnings(block_map))
    return diagnostics


def block_output_ports(block: dict[str, Any]) -> dict[str, str]:
    block_type = block.get("type")
    config = block.get("config") or {}
    if block_type == "date_filter":
        return {"value": "date_range"}
    if block_type == "period_selector":
        return {"current": "date_range", "previous": "date_range", "pair": "compare_ranges"}
    if block_type == "semantic_query":
        queries = config.get("queries")
        names = list(queries) if isinstance(queries, dict) else []
        ports: dict[str, str] = {}
        for name in names:
            ports[str(name)] = "rows"
            if config.get("compare"):
                ports[f"{name}_previous"] = "rows"
        return ports
    if block_type in {"graph", "table"}:
        return {"data": "rows"}
    return {}


def block_input_ports(block: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    block_type = block.get("type")
    config = block.get("config") or {}
    if block_type == "semantic_query":
        ports = {"date_range": ("date_range", False)}
        if config.get("compare"):
            ports["compare"] = ("compare_ranges", True)
        return ports
    if block_type in {"graph", "table"}:
        data_required = not isinstance(config.get("query"), dict)
        return {
            "data": ("rows", data_required),
            "date_range": ("date_range", False),
        }
    if block_type == "stat":
        return {"current": ("rows", True), "previous": ("rows", False)}
    return {}


def collect_query_specs(doc: Any) -> list[dict[str, Any]]:
    """Return executable query specs with block locations.

    ``query_key`` is stable within an artifact version and matches output refs
    for semantic_query blocks: ``<block_id>.<query_name>``.
    """
    entries: list[dict[str, Any]] = []
    for block in normalize_doc(doc).get("blocks", []):
        if not isinstance(block, dict):
            continue
        block_id = block.get("id")
        block_type = block.get("type")
        config = block.get("config") or {}
        if not isinstance(block_id, str) or not isinstance(config, dict):
            continue
        if block_type == "semantic_query":
            queries = config.get("queries")
            if isinstance(queries, dict):
                for name, query in queries.items():
                    if isinstance(name, str) and isinstance(query, dict):
                        entries.append(
                            {
                                "query_key": f"{block_id}.{name}",
                                "output_ref": f"{block_id}.{name}",
                                "query_name": name,
                                "query": query,
                                "block_id": block_id,
                                "block_type": block_type,
                                "path": f"blocks.{block_id}.config.queries.{name}",
                                "date_range_bound": _has_input_ref(block, "date_range"),
                                "compare_bound": bool(config.get("compare")),
                            }
                        )
        elif block_type in {"graph", "table"} and isinstance(config.get("query"), dict):
            entries.append(
                {
                    "query_key": f"{block_id}.data",
                    "output_ref": f"{block_id}.data",
                    "query_name": "data",
                    "query": config["query"],
                    "block_id": block_id,
                    "block_type": block_type,
                    "path": f"blocks.{block_id}.config.query",
                    "date_range_bound": _has_input_ref(block, "date_range"),
                    "compare_bound": False,
                }
            )
    return entries


def query_diagnostics(
    query: Any,
    *,
    block_id: str | None = None,
    path: str = "query",
    require_time_dimension: bool = False,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if not isinstance(query, dict):
        return [
            problem(
                f"{path} must be an object",
                block_id=block_id,
                code="query_shape",
            )
        ]

    raw_keys = sorted(k for k in query if k in RAW_QUERY_KEYS)
    if raw_keys:
        diagnostics.append(
            problem(
                f"{path} uses raw SQL/Cube key(s): {', '.join(raw_keys)}",
                block_id=block_id,
                code="raw_query_key",
            )
        )
    unknown_keys = sorted(k for k in query if k not in ALLOWED_QUERY_KEYS and k not in RAW_QUERY_KEYS)
    if unknown_keys:
        diagnostics.append(
            problem(
                f"{path} has unsupported key(s): {', '.join(unknown_keys)}",
                block_id=block_id,
                code="unknown_query_key",
            )
        )

    measures = _string_list(query.get("measures"))
    dimensions = _string_list(query.get("dimensions"))
    time_dimension = query.get("time_dimension")
    if query.get("measures") is not None and measures is None:
        diagnostics.append(problem(f"{path}.measures must be a string array", block_id=block_id, code="query_measures"))
    if query.get("dimensions") is not None and dimensions is None:
        diagnostics.append(problem(f"{path}.dimensions must be a string array", block_id=block_id, code="query_dimensions"))
    if time_dimension is not None and not isinstance(time_dimension, str):
        diagnostics.append(problem(f"{path}.time_dimension must be a string", block_id=block_id, code="query_time_dimension"))
    if require_time_dimension and not time_dimension:
        diagnostics.append(
            problem(
                f"{path} is bound to a date range but has no time_dimension",
                block_id=block_id,
                code="query_window_without_time_dimension",
            )
        )
    granularity = query.get("granularity")
    if granularity is not None and granularity not in ALLOWED_GRANULARITIES:
        diagnostics.append(problem(f"{path}.granularity is unsupported", block_id=block_id, code="query_granularity"))
    if granularity and not time_dimension:
        diagnostics.append(
            problem(
                f"{path}.granularity requires time_dimension",
                block_id=block_id,
                code="query_granularity_without_time_dimension",
            )
        )
    if not (measures or dimensions or time_dimension):
        diagnostics.append(
            problem(
                f"{path} must include at least one measure, dimension, or time_dimension",
                block_id=block_id,
                code="query_empty",
            )
        )
    diagnostics.extend(_filter_diagnostics(query.get("filters"), block_id=block_id, path=path))
    diagnostics.extend(_order_by_diagnostics(query.get("order_by"), block_id=block_id, path=path))
    return diagnostics


def expected_result_keys(query: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    time_dimension = query.get("time_dimension")
    if time_dimension and query.get("granularity"):
        keys.add("date")
    elif isinstance(time_dimension, str) and time_dimension:
        keys.add(member_to_key(time_dimension))
    for member in _string_list(query.get("dimensions")) or []:
        keys.add(member_to_key(member))
    for member in _string_list(query.get("measures")) or []:
        keys.add(member_to_key(member))
    return keys


def member_to_key(member: str) -> str:
    key = member.replace(".", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key


def apply_ops(doc: Any, ops: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(ops, list):
        raise GraphDocError("ops must be a list")
    original = normalize_doc(doc)
    before = validate_doc(original)
    updated = copy.deepcopy(original)
    for op in ops:
        _apply_op(updated, op)
    after = validate_doc(updated)
    introduced = introduced_diagnostics(before, after)
    if introduced:
        messages = "; ".join(d["message"] for d in introduced[:5])
        raise GraphDocError(f"Graph edit introduced diagnostics: {messages}")
    return updated


def introduced_diagnostics(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    before_keys = {_diagnostic_key(item) for item in before}
    return [item for item in after if _diagnostic_key(item) not in before_keys]


def diagnostics_have_errors(diagnostics: list[dict[str, Any]]) -> bool:
    return any(item.get("severity", "error") == "error" for item in diagnostics)


def _diagnostic_key(item: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (item.get("severity"), item.get("block_id"), item.get("code") or item.get("message"))


def _validate_block_config(block: dict[str, Any]) -> list[dict[str, Any]]:
    block_id = block["id"]
    block_type = block["type"]
    config = block.get("config") or {}
    diagnostics: list[dict[str, Any]] = []
    if block_type == "semantic_query":
        queries = config.get("queries")
        if not isinstance(queries, dict) or not queries:
            diagnostics.append(
                problem(
                    "semantic_query blocks require config.queries",
                    block_id=block_id,
                    code="semantic_query_queries",
                )
            )
        elif not all(isinstance(name, str) and name for name in queries):
            diagnostics.append(
                problem(
                    "semantic_query query names must be non-empty strings",
                    block_id=block_id,
                    code="semantic_query_names",
                )
            )
        else:
            require_time = bool(config.get("compare")) or _has_input_ref(block, "date_range")
            for name, query in queries.items():
                diagnostics.extend(
                    query_diagnostics(
                        query,
                        block_id=block_id,
                        path=f"queries.{name}",
                        require_time_dimension=require_time,
                    )
                )
    if block_type in {"graph", "table"} and isinstance(config.get("query"), dict):
        diagnostics.extend(
            query_diagnostics(
                config["query"],
                block_id=block_id,
                path="config.query",
                require_time_dimension=_has_input_ref(block, "date_range"),
            )
        )
    if block_type == "graph" and "recharts" in config:
        diagnostics.extend(
            _recharts_diagnostics(
                config.get("recharts"),
                block_id=block_id,
                path="config.recharts",
            )
        )
    return diagnostics


def _recharts_diagnostics(
    node: Any,
    *,
    block_id: str,
    path: str,
) -> list[dict[str, Any]]:
    if not isinstance(node, dict):
        return [
            problem(
                f"{path} must be an object",
                block_id=block_id,
                code="recharts_shape",
            )
        ]

    diagnostics: list[dict[str, Any]] = []
    node_type = node.get("type")
    if not isinstance(node_type, str) or not node_type:
        diagnostics.append(
            problem(
                f"{path}.type must be a non-empty string",
                block_id=block_id,
                code="recharts_type",
            )
        )
    elif node_type not in RECHARTS_COMPONENT_TYPES:
        diagnostics.append(
            problem(
                f'Unsupported Recharts component "{node_type}"',
                block_id=block_id,
                code="recharts_type",
            )
        )

    props = node.get("props", {})
    if props is not None and not isinstance(props, dict):
        diagnostics.append(
            problem(
                f"{path}.props must be an object",
                block_id=block_id,
                code="recharts_props",
            )
        )
    elif isinstance(props, dict):
        if node_type in RECHARTS_DATA_TYPES and "data" in props:
            diagnostics.append(
                problem(
                    f"{path}.props.data is not supported; bind graph inputs.data and omit props.data",
                    block_id=block_id,
                    code="recharts_data_prop",
                )
            )
        for prop_name in RECHARTS_RESULT_KEY_PROPS:
            if prop_name in props and not isinstance(props[prop_name], str):
                diagnostics.append(
                    problem(
                        f"{path}.props.{prop_name} must be a string",
                        block_id=block_id,
                        code="recharts_key_prop",
                    )
                )

    children = node.get("children", [])
    if children is None:
        return diagnostics
    if not isinstance(children, list):
        diagnostics.append(
            problem(
                f"{path}.children must be an array",
                block_id=block_id,
                code="recharts_children",
            )
        )
        return diagnostics
    for index, child in enumerate(children):
        diagnostics.extend(
            _recharts_diagnostics(
                child,
                block_id=block_id,
                path=f"{path}.children[{index}]",
            )
        )
    return diagnostics


def _validate_bindings(
    block: dict[str, Any],
    outputs: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    block_id = block["id"]
    inputs = block.get("inputs") or {}
    ports = block_input_ports(block)
    diagnostics: list[dict[str, Any]] = []
    refs: list[str] = []
    if not isinstance(inputs, dict):
        return [problem("inputs must be an object", block_id=block_id, code="inputs_shape")], refs

    for name, (_port_type, required) in ports.items():
        if required and name not in inputs:
            diagnostics.append(
                problem(
                    f'Required input "{name}" is not bound',
                    block_id=block_id,
                    code="required_input_missing",
                )
            )

    for name, binding in inputs.items():
        expected = ports.get(name)
        if expected is None:
            diagnostics.append(
                problem(
                    f'Input "{name}" is not supported by {block.get("type")}',
                    block_id=block_id,
                    code="unknown_input",
                    severity="warning",
                )
            )
            continue
        if not isinstance(binding, dict):
            diagnostics.append(
                problem(
                    f'Input "{name}" binding must be an object',
                    block_id=block_id,
                    code="binding_shape",
                )
            )
            continue
        has_ref = "$ref" in binding
        has_value = "value" in binding
        if has_ref == has_value:
            diagnostics.append(
                problem(
                    f'Input "{name}" must contain exactly one of "$ref" or "value"',
                    block_id=block_id,
                    code="binding_shape",
                )
            )
            continue
        if has_value:
            continue
        ref = binding.get("$ref")
        if not isinstance(ref, str):
            diagnostics.append(
                problem(
                    f'Input "{name}" $ref must be a string',
                    block_id=block_id,
                    code="binding_ref_shape",
                )
            )
            continue
        producer_id, port = parse_ref(ref)
        producer_outputs = outputs.get(producer_id)
        if producer_outputs is None or port not in producer_outputs:
            diagnostics.append(
                problem(
                    f'Input "{name}" references missing output "{ref}"',
                    block_id=block_id,
                    code="missing_ref",
                )
            )
            continue
        produced_type = producer_outputs[port]
        expected_type = expected[0]
        if produced_type != expected_type and "json" not in {produced_type, expected_type}:
            diagnostics.append(
                problem(
                    f'Input "{name}" expects {expected_type} but "{ref}" produces {produced_type}',
                    block_id=block_id,
                    code="port_type_mismatch",
                )
            )
            continue
        refs.append(ref)
    return diagnostics, refs


def parse_ref(ref: str) -> tuple[str, str]:
    if "." not in ref:
        return ref, ""
    block_id, port = ref.split(".", 1)
    return block_id, port


def _cycle_diagnostics(refs_by_block: dict[str, list[str]]) -> list[dict[str, Any]]:
    edges = {
        block_id: [parse_ref(ref)[0] for ref in refs]
        for block_id, refs in refs_by_block.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(block_id: str) -> None:
        if block_id in visited:
            return
        if block_id in visiting:
            start = stack.index(block_id) if block_id in stack else 0
            cycles.append([*stack[start:], block_id])
            return
        visiting.add(block_id)
        stack.append(block_id)
        for dep in edges.get(block_id, []):
            if dep in edges:
                visit(dep)
        stack.pop()
        visiting.remove(block_id)
        visited.add(block_id)

    for block_id in edges:
        visit(block_id)

    if not cycles:
        return []
    members = sorted({item for cycle in cycles for item in cycle})
    return [
        problem(
            f"Circular dependency between blocks: {', '.join(members)}",
            code="cycle",
        )
    ]


def _key_contract_warnings(block_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    query_by_output = {
        entry["output_ref"]: entry["query"]
        for entry in collect_query_specs({"blocks": list(block_map.values())})
    }
    diagnostics: list[dict[str, Any]] = []
    for block in block_map.values():
        block_type = block.get("type")
        if block_type not in {"graph", "table", "stat"}:
            continue
        config = block.get("config") or {}
        query = None
        if isinstance(config.get("query"), dict):
            query = config["query"]
        else:
            input_name = "current" if block_type == "stat" else "data"
            binding = (block.get("inputs") or {}).get(input_name)
            if isinstance(binding, dict) and isinstance(binding.get("$ref"), str):
                query = query_by_output.get(binding["$ref"])
        if not isinstance(query, dict):
            continue
        expected = expected_result_keys(query)
        if not expected:
            continue
        for key in _referenced_data_keys(block):
            if key and key not in expected:
                diagnostics.append(
                    problem(
                        f'Data key "{key}" is not produced by the bound query',
                        block_id=block["id"],
                        code=f"missing_result_key:{key}",
                        severity="warning",
                    )
                )
    return diagnostics


def _referenced_data_keys(block: dict[str, Any]) -> list[str]:
    config = block.get("config") or {}
    block_type = block.get("type")
    keys: list[str] = []
    if block_type == "graph":
        if isinstance(config.get("x_key"), str):
            keys.append(config["x_key"])
        if isinstance(config.get("y_key"), str):
            keys.append(config["y_key"])
        series = config.get("series")
        if isinstance(series, list):
            for item in series:
                if isinstance(item, str):
                    keys.append(item)
                elif isinstance(item, dict):
                    value = item.get("data_key") or item.get("y_key") or item.get("key")
                    if isinstance(value, str):
                        keys.append(value)
    if block_type == "table":
        columns = config.get("columns")
        if isinstance(columns, list):
            for item in columns:
                if isinstance(item, str):
                    keys.append(item)
                elif isinstance(item, dict):
                    value = item.get("key") or item.get("accessor")
                    if isinstance(value, str):
                        keys.append(value)
    if block_type == "stat":
        for field in ("value_key", "value_path", "delta_path"):
            value = config.get(field)
            if isinstance(value, str):
                keys.append(_path_key(value))
    return keys


_PATH_SEGMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _path_key(value: str) -> str:
    matches = _PATH_SEGMENT_RE.findall(value)
    return matches[-1] if matches else value


def _filter_diagnostics(
    filters: Any,
    *,
    block_id: str | None,
    path: str,
) -> list[dict[str, Any]]:
    if filters is None:
        return []
    if not isinstance(filters, list):
        return [problem(f"{path}.filters must be an array", block_id=block_id, code="query_filters")]
    diagnostics: list[dict[str, Any]] = []
    for index, item in enumerate(filters):
        if not isinstance(item, dict):
            diagnostics.append(problem(f"{path}.filters[{index}] must be an object", block_id=block_id, code="query_filter_shape"))
            continue
        if not isinstance(item.get("field"), str) or not item.get("field"):
            diagnostics.append(problem(f"{path}.filters[{index}] needs field", block_id=block_id, code="query_filter_field"))
        if "member" in item:
            diagnostics.append(problem(f"{path}.filters[{index}] uses member; use field", block_id=block_id, code="query_filter_member_key"))
    return diagnostics


def _order_by_diagnostics(
    order_by: Any,
    *,
    block_id: str | None,
    path: str,
) -> list[dict[str, Any]]:
    if order_by is None:
        return []
    if not isinstance(order_by, list):
        return [problem(f"{path}.order_by must be an array", block_id=block_id, code="query_order_by")]
    diagnostics: list[dict[str, Any]] = []
    for index, item in enumerate(order_by):
        if not isinstance(item, dict):
            diagnostics.append(problem(f"{path}.order_by[{index}] must be an object", block_id=block_id, code="query_order_shape"))
            continue
        if not isinstance(item.get("field"), str) or not item.get("field"):
            diagnostics.append(problem(f"{path}.order_by[{index}] needs field", block_id=block_id, code="query_order_field"))
    return diagnostics


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _has_input_ref(block: dict[str, Any], input_name: str) -> bool:
    binding = (block.get("inputs") or {}).get(input_name)
    return isinstance(binding, dict) and "$ref" in binding


def _apply_op(doc: dict[str, Any], op: dict[str, Any]) -> None:
    if not isinstance(op, dict):
        raise GraphDocError("Each op must be an object")
    kind = op.get("op")
    if kind == "add_block":
        block = op.get("block")
        if not isinstance(block, dict):
            raise GraphDocError("add_block requires block")
        blocks = doc.setdefault("blocks", [])
        index = _insert_index(blocks, op.get("after", "end"))
        blocks.insert(index, copy.deepcopy(block))
        return
    if kind == "remove_block":
        block_id = op.get("id")
        blocks = doc.get("blocks", [])
        doc["blocks"] = [block for block in blocks if block.get("id") != block_id]
        return
    if kind == "move_block":
        block_id = op.get("id")
        blocks = doc.get("blocks", [])
        matches = [block for block in blocks if block.get("id") == block_id]
        if not matches:
            raise GraphDocError(f'Block "{block_id}" not found')
        block = matches[0]
        remaining = [item for item in blocks if item.get("id") != block_id]
        index = _insert_index(remaining, op.get("after", "end"))
        remaining.insert(index, block)
        doc["blocks"] = remaining
        return
    if kind == "set":
        target = op.get("target")
        if not isinstance(target, str):
            raise GraphDocError("set requires target")
        _set_target(doc, target, op.get("value"))
        return
    raise GraphDocError(f"Unsupported op {kind!r}")


def _insert_index(blocks: list[dict[str, Any]], after: Any) -> int:
    if after in (None, "end"):
        return len(blocks)
    if after == "start":
        return 0
    for index, block in enumerate(blocks):
        if block.get("id") == after:
            return index + 1
    raise GraphDocError(f'after block "{after}" not found')


def _set_target(doc: dict[str, Any], target: str, value: Any) -> None:
    if target == "story/name":
        doc["name"] = value
        return
    if target == "story/prd":
        doc["prd"] = value
        return
    if target == "story/tags":
        doc["tags"] = value
        return
    if not target.startswith("block/"):
        raise GraphDocError(f"Unsupported set target {target!r}")
    _prefix, block_id, *path = target.split("/")
    if not block_id or not path:
        raise GraphDocError("block set target must include a path")
    block = _find_block(doc, block_id)
    cursor: Any = block
    for part in path[:-1]:
        if not isinstance(cursor, dict):
            raise GraphDocError(f"Cannot set through non-object path segment {part!r}")
        cursor = cursor.setdefault(part, {})
    if not isinstance(cursor, dict):
        raise GraphDocError("Cannot set target on non-object")
    cursor[path[-1]] = value


def _find_block(doc: dict[str, Any], block_id: str) -> dict[str, Any]:
    for block in doc.get("blocks", []):
        if isinstance(block, dict) and block.get("id") == block_id:
            return block
    raise GraphDocError(f'Block "{block_id}" not found')
