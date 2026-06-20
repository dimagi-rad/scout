"""The trigger-agnostic engine for cross-opp canonical measures.

Spec in -> resolve across the workspace's opps -> classify doubt -> commit (additive
model regen + lineage + Cube reload) or hand back for approval. Fed by both the
on-demand agent tool and the app-driven proposer.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from asgiref.sync import sync_to_async
from django.db import transaction

from apps.transformations.models import CrossOppMeasure, CrossOppMeasureLineage
from apps.transformations.services.crossopp_cube_builder import (
    OppRef,
    render_crossopp_model,
)
from apps.transformations.services.measure_resolver import MeasureResolution
from apps.workspaces.services.schema_manager import SchemaManager

_DOUBT_STATUSES = frozenset({"low_confidence", "absent"})


def serialize_resolution(r: MeasureResolution) -> dict:
    return asdict(r)


def deserialize_resolution(d: dict) -> MeasureResolution:
    return MeasureResolution(**d)


def classify_doubt(
    resolutions: dict[str, MeasureResolution],
) -> tuple[bool, list[str]]:
    """Doubt = any opp the resolver was unsure about (low_confidence) or found absent."""
    flagged = [opp for opp, r in resolutions.items() if r.status in _DOUBT_STATUSES]
    return (bool(flagged), flagged)


BLENDED_CUBE = "kmc_cross_opp"


def _ws_hash(workspace) -> str:
    return SchemaManager()._view_schema_name(workspace.id)


def load_workspace_specs_and_resolutions(workspace):
    """Reconstruct (specs, resolutions_by_opp) from the persisted catalog + lineage.

    Lets a single add be additive: re-render the whole model from what already exists
    plus the new measure.
    """
    specs = [m.to_spec() for m in CrossOppMeasure.objects.filter(workspace=workspace)]
    res: dict[str, dict] = {}
    for row in CrossOppMeasureLineage.objects.filter(workspace=workspace):
        res.setdefault(row.opportunity_id, {})[row.measure] = MeasureResolution(
            measure=row.measure, column=row.column or None, source_path=row.source_path or None,
            sql_expression=row.sql_expression or None, confidence=row.confidence,
            status=row.status, matched_label=row.matched_label, reason="",
        )
    return specs, res


def add_measure(workspace, spec, resolutions, opps, *, model_root="cube/model"):
    """Commit ONE measure: upsert spec + lineage, regenerate the full model additively, write it.

    Returns the inspector-shaped lineage list for this measure.
    """
    with transaction.atomic():
        CrossOppMeasure.objects.update_or_create(
            workspace=workspace, name=spec.name,
            defaults={"description": spec.description, "kind": spec.kind},
        )
        for opp_id, r in resolutions.items():
            CrossOppMeasureLineage.objects.update_or_create(
                workspace=workspace, opportunity_id=opp_id, measure=spec.name,
                defaults={
                    "column": r.column or "", "source_path": r.source_path or "",
                    "matched_label": r.matched_label or "", "sql_expression": r.sql_expression or "",
                    "confidence": r.confidence, "status": r.status,
                },
            )

    specs, res_by_opp = load_workspace_specs_and_resolutions(workspace)
    model_yaml = render_crossopp_model(BLENDED_CUBE, opps, specs, res_by_opp)
    ws_hash = _ws_hash(workspace)
    path = Path(model_root) / ws_hash / "canonical.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model_yaml)

    return [
        {
            "opportunity_id": opp_id, "status": r.status, "confidence": r.confidence,
            "column": r.column, "matched_label": r.matched_label, "sql_expression": r.sql_expression,
        }
        for opp_id, r in resolutions.items()
    ]


aadd_measure = sync_to_async(add_measure)

from apps.transformations.services.measure_resolver import (  # noqa: E402,I001
    gather_measure_candidates, resolve_measure, _clinical_entry_candidates,
)
from apps.users.models import TenantMembership  # noqa: E402
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant  # noqa: E402

LABS_PROVIDER = "commcare_connect_labs"


def workspace_opps(workspace):
    """Active opps in the workspace + their stg_visits field candidates (sync ORM)."""
    opps, cands = [], {}
    for wt in WorkspaceTenant.objects.filter(workspace=workspace).select_related("tenant"):
        tenant = wt.tenant
        schema = TenantSchema.objects.filter(tenant=tenant, state=SchemaState.ACTIVE).first()
        if schema is None:
            continue
        tm = TenantMembership.objects.filter(tenant=tenant).first()
        form_defs = (getattr(getattr(tm, "metadata", None), "metadata", None) or {}).get(
            "form_definitions", {}
        )
        opps.append(OppRef(tenant.external_id, schema.schema_name))
        cands[tenant.external_id] = gather_measure_candidates(form_defs)
    return opps, cands


async def resolve_across_opps_from_candidates(spec, candidates_by_opp, *, model_client=None):
    out = {}
    for opp_id, cands in candidates_by_opp.items():
        out[opp_id] = await resolve_measure(spec, cands, model_client=model_client)
    return out


def shortlist_for_opp(candidates) -> list[dict]:
    return [
        {"column": c.column, "label": c.label, "type": c.type}
        for c in _clinical_entry_candidates(candidates)
    ]
