"""Semantic query dependency manifests for graph artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactSemanticQuery

from .graph_doc import (
    collect_query_specs,
    expected_result_keys,
    normalize_doc,
    query_diagnostics,
    validate_doc,
)

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_SOURCE = "graph-doc:v1"


def build_semantic_query_manifest(doc: Any) -> dict[str, Any]:
    doc = normalize_doc(doc)
    doc_diagnostics = validate_doc(doc)
    entries = [_manifest_entry(item) for item in collect_query_specs(doc)]
    unresolved = []
    for diagnostic in doc_diagnostics:
        if diagnostic.get("severity", "error") == "error":
            unresolved.append(
                {
                    "kind": diagnostic.get("code") or "graph_doc",
                    "detail": diagnostic.get("message", "Graph doc diagnostic"),
                    "block_id": diagnostic.get("block_id"),
                }
            )
    for entry in entries:
        for reference in entry["unresolved_references"]:
            unresolved.append({**reference, "query_key": entry["key"]})
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": timezone.now().isoformat(),
        "source": MANIFEST_SOURCE,
        "entries": entries,
        "unresolved": unresolved,
    }


def sync_artifact_semantic_query_manifest(artifact: Artifact) -> dict[str, Any]:
    """Regenerate and persist manifest rows for one artifact version."""
    story_doc = (artifact.data or {}).get("story_doc") if isinstance(artifact.data, dict) else None
    manifest = build_semantic_query_manifest(story_doc or {})
    compatibility_queries = [
        {"name": entry["key"], **entry["query"]}
        for entry in manifest["entries"]
        if entry.get("validation_status") == "valid"
    ]
    if not compatibility_queries and not manifest["entries"] and artifact.semantic_queries:
        compatibility_queries = artifact.semantic_queries
    with transaction.atomic():
        Artifact.objects.filter(pk=artifact.pk).update(
            semantic_query_manifest=manifest,
            semantic_queries=compatibility_queries,
        )
        ArtifactSemanticQuery.objects.filter(artifact=artifact).delete()
        records = [
            ArtifactSemanticQuery(
                artifact=artifact,
                workspace=artifact.workspace,
                query_key=entry["key"],
                query_hash=entry["query_hash"],
                query_type=entry["query_type"],
                validation_status=entry["validation_status"],
                query_payload=entry["query"],
                members=entry["members"],
                datasets=entry["datasets"],
                dependencies=entry["dependencies"],
                block_locations=entry["block_locations"],
                unresolved_references=entry["unresolved_references"],
            )
            for entry in manifest["entries"]
            if artifact.workspace_id
        ]
        if records:
            ArtifactSemanticQuery.objects.bulk_create(records)
    artifact.semantic_query_manifest = manifest
    artifact.semantic_queries = compatibility_queries
    return manifest


def semantic_query_summary(record: ArtifactSemanticQuery) -> dict[str, Any]:
    return {
        "query_key": record.query_key,
        "query_hash": record.query_hash,
        "query_type": record.query_type,
        "validation_status": record.validation_status,
        "query_payload": record.query_payload,
        "members": record.members,
        "datasets": record.datasets,
        "dependencies": record.dependencies,
        "block_locations": record.block_locations,
        "unresolved_references": record.unresolved_references,
    }


def _manifest_entry(item: dict[str, Any]) -> dict[str, Any]:
    query = _canonical_query(item["query"])
    diagnostics = query_diagnostics(
        query,
        block_id=item["block_id"],
        path=item["path"],
        require_time_dimension=bool(item.get("date_range_bound") or item.get("compare_bound")),
    )
    unresolved = [
        {
            "kind": diagnostic.get("code") or "query",
            "detail": diagnostic.get("message", "Query diagnostic"),
            "block_id": diagnostic.get("block_id"),
            "path": item["path"],
        }
        for diagnostic in diagnostics
        if diagnostic.get("severity", "error") == "error"
    ]
    members = _query_members(query)
    datasets = sorted({member.split(".", 1)[0] for member in members if "." in member})
    dependencies = [
        {"kind": "dataset", "name": dataset}
        for dataset in datasets
    ] + [{"kind": "semantic_member", "name": member} for member in members]
    return {
        "key": item["query_key"],
        "query_key": item["query_key"],
        "query_hash": _query_hash(query),
        "query_type": "semantic",
        "confidence": "high",
        "validation_status": "invalid" if unresolved else "valid",
        "query": query,
        "members": members,
        "result_keys": sorted(_result_keys_for_manifest(query)),
        "datasets": datasets,
        "dependencies": dependencies,
        "block_locations": [
            {
                "block_id": item["block_id"],
                "block_type": item["block_type"],
                "path": item["path"],
                "output_ref": item["output_ref"],
            }
        ],
        "unresolved_references": unresolved,
    }


def _canonical_query(query: dict[str, Any]) -> dict[str, Any]:
    canonical = json.loads(json.dumps(query, sort_keys=True, default=str))
    if "limit" not in canonical or canonical.get("limit") in (None, ""):
        canonical["limit"] = 100
    return canonical


def _query_hash(query: dict[str, Any]) -> str:
    payload = json.dumps(query, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _query_members(query: dict[str, Any]) -> list[str]:
    members: list[str] = []
    for key in ("measures", "dimensions"):
        value = query.get(key)
        if isinstance(value, list):
            members.extend(item for item in value if isinstance(item, str))
    time_dimension = query.get("time_dimension")
    if isinstance(time_dimension, str) and time_dimension:
        members.append(time_dimension)
    filters = query.get("filters")
    if isinstance(filters, list):
        for item in filters:
            if isinstance(item, dict) and isinstance(item.get("field"), str):
                members.append(item["field"])
    order_by = query.get("order_by")
    if isinstance(order_by, list):
        for item in order_by:
            if isinstance(item, dict) and isinstance(item.get("field"), str):
                members.append(item["field"])
    return sorted(dict.fromkeys(members))


def _result_keys_for_manifest(query: dict[str, Any]) -> set[str]:
    return expected_result_keys(query)
