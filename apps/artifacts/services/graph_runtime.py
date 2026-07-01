"""Server-side runtime checks for graph artifacts."""

from __future__ import annotations

from typing import Any

from apps.semantic.services.query import run_semantic_query

from .graph_doc import expected_result_keys, member_to_key, normalize_doc, validate_doc
from .graph_manifest import build_semantic_query_manifest

CHECK_ROW_LIMIT = 50
MAX_CHECK_QUERIES = 25


async def check_graph_artifact(artifact, *, user_id: str = "") -> dict[str, Any]:
    doc = normalize_doc((artifact.data or {}).get("story_doc") if isinstance(artifact.data, dict) else {})
    diagnostics = validate_doc(doc)
    manifest = build_semantic_query_manifest(doc)
    entries = manifest.get("entries", [])[:MAX_CHECK_QUERIES]
    query_results = []
    actual_keys: dict[str, list[str]] = {}
    for entry in entries:
        query = dict(entry.get("query") or {})
        query.setdefault("limit", CHECK_ROW_LIMIT)
        result = await run_semantic_query(artifact.workspace, query, user_id=user_id)
        if not result.get("success", True) or result.get("error"):
            error = result.get("error")
            message = error.get("message") if isinstance(error, dict) else str(error)
            query_results.append(
                {
                    "query_key": entry["key"],
                    "status": "error",
                    "error": message or "Semantic query failed",
                    "semantic_query": query,
                }
            )
            continue
        row_keys = _row_keys(result.get("columns", []), result.get("rows", []), query)
        actual_keys[entry["key"]] = sorted(row_keys)
        query_results.append(
            {
                "query_key": entry["key"],
                "status": "ok",
                "row_count": result.get("row_count", 0),
                "result_keys": sorted(row_keys),
                "truncated": result.get("truncated", False),
                "semantic_query": result.get("semantic_query", query),
            }
        )
    key_warnings = _key_contract_warnings(manifest.get("entries", []), actual_keys)
    ok_count = sum(1 for item in query_results if item["status"] == "ok")
    return {
        "success": not diagnostics and not key_warnings and ok_count == len(query_results),
        "diagnostics": diagnostics,
        "manifest": {
            "schema_version": manifest.get("schema_version"),
            "entry_count": len(manifest.get("entries", [])),
            "unresolved_count": len(manifest.get("unresolved", [])),
        },
        "queries": query_results,
        "key_warnings": key_warnings,
        "summary": f"{ok_count}/{len(query_results)} queries ok",
    }


def _row_keys(columns: Any, rows: Any, query: dict[str, Any]) -> set[str]:
    if not rows:
        return expected_result_keys(query)
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict):
            return {_normalize_key(key, query) for key in first}
        if isinstance(first, list) and isinstance(columns, list):
            return {_normalize_key(str(key), query) for key in columns}
    return expected_result_keys(query)


def _normalize_key(key: str, query: dict[str, Any]) -> str:
    if query.get("time_dimension") and query.get("granularity"):
        bucket_keys = {
            member_to_key(str(query["time_dimension"])),
            f"{member_to_key(str(query['time_dimension']))}_{query['granularity']}",
            "date",
        }
        if key.replace(".", "_").replace("__", "_") in bucket_keys:
            return "date"
    normalized = key.replace(".", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


def _key_contract_warnings(
    entries: list[dict[str, Any]],
    actual_keys: dict[str, list[str]],
) -> list[dict[str, Any]]:
    warnings = []
    for entry in entries:
        query_key = entry.get("key")
        keys = set(actual_keys.get(query_key, []))
        if not keys:
            continue
        for expected in entry.get("result_keys", []):
            if expected not in keys:
                warnings.append(
                    {
                        "query_key": query_key,
                        "message": f'Expected result key "{expected}" was not returned',
                        "expected_key": expected,
                        "actual_keys": sorted(keys),
                    }
                )
    return warnings
