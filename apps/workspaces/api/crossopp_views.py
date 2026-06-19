"""Cross-opp transparency inspector API.

Powers "confirm Cube's number": for a cross-opp workspace, returns each canonical measure's
per-opp resolution lineage — which app field it resolved to, the human label it matched on,
the confidence, the status, and the per-opp SQL expression — plus the full generated Cube
model (the actual union SQL Cube runs). Everything a user needs to verify the analysis.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from asgiref.sync import async_to_sync
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.transformations.models import CrossOppMeasureLineage
from apps.workspaces.models import Workspace
from apps.workspaces.services.schema_manager import SchemaManager
from mcp_server.services.semantic import semantic_query


def _blended_cube_and_measures(model_yaml: str) -> tuple[str | None, list[str]]:
    """Find the blended cube (the one carrying measures) + its measure names."""
    data = yaml.safe_load(model_yaml) or {}
    for cube in data.get("cubes", []):
        if cube.get("measures"):
            return cube["name"], [m["name"] for m in cube["measures"]]
    return None, []


def dashboard_query_sql(model_yaml: str) -> str | None:
    """Build the cross-opp Semantic SQL: every measure sliced by opportunity_id."""
    name, measures = _blended_cube_and_measures(model_yaml)
    if not name:
        return None
    cols = ["opportunity_id", *[f"MEASURE({m})" for m in measures]]
    return (
        f"SELECT {', '.join(cols)} FROM {name} "
        "GROUP BY opportunity_id ORDER BY opportunity_id"
    )


def inspector_payload(workspace: Workspace) -> dict:
    """Assemble the transparency payload for a cross-opp workspace (pure; no auth)."""
    measures: dict[str, dict] = {}
    rows = CrossOppMeasureLineage.objects.filter(workspace=workspace).order_by(
        "measure", "opportunity_id"
    )
    for r in rows:
        entry = measures.setdefault(r.measure, {"measure": r.measure, "opps": []})
        entry["opps"].append(
            {
                "opportunity_id": r.opportunity_id,
                "status": r.status,
                "confidence": r.confidence,
                "column": r.column,
                "source_path": r.source_path,  # the CommCare question xpath it came from
                "matched_label": r.matched_label,  # the human label the resolver matched on
                "sql_expression": r.sql_expression,
            }
        )

    # Per-measure coverage summary (how many opps resolved vs absent / low-confidence).
    for entry in measures.values():
        statuses = [o["status"] for o in entry["opps"]]
        entry["coverage"] = {
            "resolved": statuses.count("resolved"),
            "low_confidence": statuses.count("low_confidence"),
            "absent": statuses.count("absent"),
            "total": len(statuses),
        }

    ws_hash = SchemaManager()._view_schema_name(workspace.id)
    model_path = Path("cube/model") / ws_hash / "canonical.yml"
    model_yaml = model_path.read_text() if model_path.exists() else ""

    return {
        "workspace_id": str(workspace.id),
        "schema_name": ws_hash,
        "measures": list(measures.values()),
        "model_yaml": model_yaml,  # the exact Cube model (per-opp cubes + blended union SQL)
    }


class CrossOppInspectorView(APIView):
    """GET /api/workspaces/<id>/crossopp/inspector/ — the trust/transparency payload."""

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = get_object_or_404(
            Workspace, id=workspace_id, memberships__user=request.user
        )
        return Response(inspector_payload(workspace))


class CrossOppDashboardView(APIView):
    """GET /api/workspaces/<id>/crossopp/dashboard/ — every measure sliced by opportunity.

    Runs the cross-opp Semantic SQL against Cube and returns the per-opportunity rows that
    the dashboard renders.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace = get_object_or_404(
            Workspace, id=workspace_id, memberships__user=request.user
        )
        ws_hash = SchemaManager()._view_schema_name(workspace.id)
        model_path = Path("cube/model") / ws_hash / "canonical.yml"
        if not model_path.exists():
            return Response({"error": "no cross-opp model for this workspace"}, status=404)
        sql = dashboard_query_sql(model_path.read_text())
        if not sql:
            return Response({"error": "no blended cube in the model"}, status=404)
        result = async_to_sync(semantic_query)(sql, workspace_id=str(workspace.id))
        return Response({"sql": sql, **result})
