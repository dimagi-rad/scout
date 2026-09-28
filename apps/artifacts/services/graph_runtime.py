"""Server-side runtime checks for graph artifacts."""

from __future__ import annotations

from typing import Any

from apps.semantic.services.date_context import DateContextError
from apps.semantic.services.query import run_semantic_query
from apps.semantic.services.query_outcomes import QueryReadiness
from apps.workspaces.models import Workspace

from .graph_doc import (
    diagnostics_have_errors,
    expected_result_keys,
    member_to_key,
    normalize_doc,
    validate_doc,
)
from .graph_manifest import build_semantic_query_manifest
from .query_context import resolve_artifact_queries

CHECK_ROW_LIMIT = 50
MAX_CHECK_QUERIES = 25


async def check_graph_artifact(artifact, *, user_id: str = "") -> dict[str, Any]:
    doc = normalize_doc(
        (artifact.data or {}).get("story_doc") if isinstance(artifact.data, dict) else {}
    )
    diagnostics = validate_doc(doc)
    manifest = build_semantic_query_manifest(doc)
    manifest_summary = {
        "schema_version": manifest.get("schema_version"),
        "entry_count": len(manifest.get("entries", [])),
        "unresolved_count": len(manifest.get("unresolved", [])),
    }
    query_results = []
    actual_keys: dict[str, list[str]] = {}
    try:
        resolved, context = resolve_artifact_queries(doc)
    except DateContextError as exc:
        return {
            "success": False,
            "diagnostics": [
                *diagnostics,
                {"severity": "error", "code": "date_context", "message": str(exc)},
            ],
            "manifest": manifest_summary,
            "queries": [],
            "key_warnings": [],
            "query_context": None,
            "failures": [
                {
                    "query_key": None,
                    "message": str(exc),
                    "category": "invalid_document",
                    "code": "VALIDATION_ERROR",
                    "retryable": False,
                    "recovery_action": None,
                }
            ],
            "summary": "Date context could not be resolved",
        }
    if len(resolved) > MAX_CHECK_QUERIES:
        return {
            "success": False,
            "diagnostics": [
                *diagnostics,
                {
                    "severity": "error",
                    "code": "query_check_limit",
                    "message": f"Runtime checks support at most {MAX_CHECK_QUERIES} queries, including both comparison periods; this artifact resolves to {len(resolved)}.",
                },
            ],
            "manifest": manifest_summary,
            "queries": [],
            "key_warnings": [],
            "query_context": context,
            "failures": [
                {
                    "query_key": None,
                    "message": "Too many queries to validate the complete artifact",
                    "category": "invalid_document",
                    "code": "VALIDATION_ERROR",
                    "retryable": False,
                    "recovery_action": None,
                }
            ],
            "summary": "Too many queries to validate the complete artifact",
        }
    entries = resolved
    if diagnostics_have_errors(diagnostics):
        return {
            "success": False,
            "diagnostics": diagnostics,
            "manifest": manifest_summary,
            "queries": [],
            "key_warnings": [],
            "failures": [
                {
                    "query_key": None,
                    "message": "Document validation failed; no queries executed.",
                    "category": "invalid_document",
                    "code": "VALIDATION_ERROR",
                    "retryable": False,
                    "recovery_action": None,
                }
            ],
            "summary": "Document validation failed; no queries executed.",
        }
    workspace = await Workspace.objects.aget(pk=artifact.workspace_id)
    queries = [{key: value for key, value in entry.items() if key != "name"} for entry in entries]
    for query in queries:
        query.setdefault("limit", CHECK_ROW_LIMIT)
    readiness = QueryReadiness(workspace, queries)
    for entry, query in zip(entries, queries, strict=True):
        result = await run_semantic_query(workspace, query, user_id=user_id, readiness=readiness)
        if not result.get("success", True) or result.get("error"):
            error = result.get("error")
            failure = _query_failure(error)
            query_results.append(
                {
                    "query_key": entry["name"],
                    "status": "error",
                    "error": failure["message"],
                    "failure": failure,
                    "semantic_query": query,
                }
            )
            continue
        row_keys = _row_keys(result.get("columns", []), result.get("rows", []), query)
        actual_keys[entry["name"]] = sorted(row_keys)
        query_results.append(
            {
                "query_key": entry["name"],
                "status": "ok",
                "row_count": result.get("row_count", 0),
                "result_keys": sorted(row_keys),
                "truncated": result.get("truncated", False),
                "semantic_query": result.get("semantic_query", query),
            }
        )
    # Check each resolved period independently; combining both key sets would
    # let a valid current result hide a broken previous-period result.
    key_warnings = _key_contract_warnings(
        [
            {"key": entry["name"], "result_keys": sorted(expected_result_keys(entry))}
            for entry in entries
        ],
        actual_keys,
    )
    ok_count = sum(1 for item in query_results if item["status"] == "ok")
    return {
        "success": not key_warnings and ok_count == len(query_results),
        "query_context": context,
        "diagnostics": diagnostics,
        "manifest": manifest_summary,
        "queries": query_results,
        "failures": [
            {"query_key": query["query_key"], **query["failure"]}
            for query in query_results
            if query["status"] == "error"
        ]
        + [
            {
                "query_key": warning.get("query_key"),
                "message": warning.get("message", ""),
                "category": "invalid_document",
                "code": "RESULT_KEY_MISMATCH",
                "retryable": False,
                "recovery_action": None,
            }
            for warning in key_warnings
        ],
        "key_warnings": key_warnings,
        "summary": f"{ok_count}/{len(query_results)} queries ok",
    }


def _query_failure(error):
    if not isinstance(error, dict):
        error = {"message": str(error)} if error else {}
    code = str(error.get("code") or "UNKNOWN")
    permission_required = code in {
        "AUTH_ACCESS_DENIED",
        "AUTH_TOKEN_EXPIRED",
        "AUTH_CREDENTIAL_MISSING",
        "WORKSPACE_TENANT_UNREACHABLE",
    }
    return {
        "code": code,
        "message": str(error.get("message") or "Semantic query failed")[:500],
        "category": "permission_required"
        if permission_required
        else error.get("category", "runtime_failure"),
        "retryable": not permission_required and error.get("retryable") is True,
        "recovery_action": None if permission_required else error.get("recovery_action"),
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
