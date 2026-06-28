"""Cross-opp transparency inspector API.

Powers "confirm Cube's number": for a cross-opp workspace, returns each canonical measure's
per-opp resolution lineage — which app field it resolved to, the human label it matched on,
the confidence, the status, and the per-opp SQL expression — plus the full generated Cube
model (the actual union SQL Cube runs). Everything a user needs to verify the analysis.
"""

# ruff: noqa: S608 — composes Cube *Semantic* SQL from model-derived cube/measure names
# (not user input), run through Cube's SQL API, not a raw parameterized DB query.

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml
from asgiref.sync import async_to_sync
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.transformations.models import CrossOppMeasureDraft, CrossOppMeasureLineage
from apps.transformations.services import crossopp_measure_service as svc
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


def _apply_overrides(draft, overrides):
    """Return resolutions dict (opp -> MeasureResolution) with the user's per-opp choices."""
    res = {o: svc.deserialize_resolution(d) for o, d in draft.resolutions.items()}
    for opp_id, choice in (overrides or {}).items():
        if opp_id not in res:
            continue
        action = choice.get("action")
        if action == "reject":
            r = res[opp_id]
            res[opp_id] = dataclasses.replace(
                r,
                measure=draft.name,
                column=None,
                source_path=None,
                sql_expression=None,
                status="absent",
                matched_label="",
                reason="user rejected",
            )
        elif action == "pick":
            col = choice.get("column")
            if not col:
                raise ValueError(f"pick action for opp {opp_id} missing column")
            shortlist_cols = {s["column"] for s in (draft.shortlists.get(opp_id) or [])}
            if col not in shortlist_cols:
                raise ValueError(
                    f"pick column {col!r} for opp {opp_id} is not in the draft shortlist"
                )
            sql = col if draft.kind == "numeric" else f"({col} = 'yes')"
            res[opp_id] = dataclasses.replace(
                res[opp_id],
                measure=draft.name,
                column=col,
                source_path=None,
                sql_expression=sql,
                confidence=1.0,
                status="resolved",
                matched_label="(user)",
                reason="user picked",
            )
        elif action == "confirm":
            r = res[opp_id]
            res[opp_id] = dataclasses.replace(
                r,
                confidence=1.0,
                status="resolved",
                reason="user confirmed",
            )
    return res


def _defer_measure_resume(workspace, thread_id, measure_name):
    from asgiref.sync import async_to_sync as _ats

    from apps.workspaces.tasks import resume_thread_after_measure_approval

    _ats(resume_thread_after_measure_approval.defer_async)(
        workspace_id=str(workspace.id), thread_id=thread_id, measure_name=measure_name
    )


class CrossOppMeasureApproveView(APIView):
    """POST /api/workspaces/<id>/crossopp/measures/<draft_id>/approve/

    Applies per-opp overrides (confirm / pick / reject) to the draft's resolutions,
    calls add_measure to commit the lineage + regenerate the Cube model, marks the draft
    committed, and defers the resume task to unblock the waiting agent thread.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, draft_id):
        workspace = get_object_or_404(Workspace, id=workspace_id, memberships__user=request.user)
        draft = get_object_or_404(CrossOppMeasureDraft, id=draft_id, workspace=workspace)
        if draft.status != "pending":
            return Response({"error": f"draft already {draft.status}"}, status=409)
        opps, _ = svc.workspace_opps(workspace)
        try:
            resolutions = _apply_overrides(draft, request.data.get("overrides", {}))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        if getattr(draft, "target", "measure") == "visit_field":
            # A redefinition of a per-visit field (e.g. age_days = visit_date - child_dob):
            # commit via add_visit_field (no CrossOppMeasure catalog entry, surfaces as a
            # cube dimension/visit-measure) and regenerate the model.
            svc.add_visit_field(workspace, draft.name, resolutions, opps)
            lineage = svc.derived_lineage(draft.name, resolutions)
        else:
            lineage = svc.add_measure(workspace, draft.to_spec_like(), resolutions, opps)
        draft.status = "committed"
        draft.save(update_fields=["status"])
        _defer_measure_resume(workspace, draft.thread_id, draft.name)
        return Response({"status": "committed", "measure": draft.name, "lineage": lineage})
