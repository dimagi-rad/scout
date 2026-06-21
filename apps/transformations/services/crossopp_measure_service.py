"""The trigger-agnostic engine for cross-opp canonical measures.

Spec in -> resolve across the workspace's opps -> classify doubt -> commit (additive
model regen + lineage + Cube reload) or hand back for approval. Fed by both the
on-demand agent tool and the app-driven proposer.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import asdict
from pathlib import Path

from asgiref.sync import sync_to_async
from django.db import transaction

from apps.transformations.models import CrossOppMeasure, CrossOppMeasureLineage
from apps.transformations.services.crossopp_cube_builder import (
    VISIT_FIELDS,
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


def regenerate_model(workspace, opps, *, model_root="cube/model"):
    """Re-render + write the full Cube model from the workspace's persisted state.

    Includes the per-visit growth surface whenever visit-field resolutions
    (VISIT_FIELDS) have been persisted as lineage — so the surface survives every
    later additive regen (e.g. a chat-defined measure), not just the build."""
    specs, res_by_opp = load_workspace_specs_and_resolutions(workspace)
    visit_present = any(any(f in r for f in VISIT_FIELDS) for r in res_by_opp.values())
    model_yaml = render_crossopp_model(
        BLENDED_CUBE, opps, specs, res_by_opp,
        visit_resolutions_by_opp=res_by_opp if visit_present else None,
    )
    path = Path(model_root) / _ws_hash(workspace) / "canonical.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model_yaml)


def add_visit_field(workspace, field_name, resolutions, opps, *, model_root="cube/model"):
    """Persist a canonical PER-VISIT field's per-opp resolutions, then regenerate the model.

    Visit fields (VISIT_FIELDS) are stored as lineage rows like measures but have no
    CrossOppMeasure catalog entry, so they surface as cube dimensions/visit-measures
    (via render's visit path) rather than as averaged measures."""
    with transaction.atomic():
        for opp_id, r in resolutions.items():
            CrossOppMeasureLineage.objects.update_or_create(
                workspace=workspace, opportunity_id=opp_id, measure=field_name,
                defaults={
                    "column": r.column or "", "source_path": r.source_path or "",
                    "matched_label": r.matched_label or "", "sql_expression": r.sql_expression or "",
                    "confidence": r.confidence, "status": r.status,
                },
            )
    regenerate_model(workspace, opps, model_root=model_root)


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

    regenerate_model(workspace, opps, model_root=model_root)

    return [
        {
            "opportunity_id": opp_id, "status": r.status, "confidence": r.confidence,
            "column": r.column, "matched_label": r.matched_label, "sql_expression": r.sql_expression,
        }
        for opp_id, r in resolutions.items()
    ]


aadd_measure = sync_to_async(add_measure)

from apps.transformations.services.measure_resolver import (  # noqa: E402,I001
    FieldCandidate, gather_measure_candidates, resolve_measure, _clinical_entry_candidates,
)
from apps.users.models import TenantMembership  # noqa: E402
from django.db import connection  # noqa: E402
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant  # noqa: E402

import asyncio  # noqa: E402
import httpx  # noqa: E402
from django.conf import settings  # noqa: E402
from mcp_server.services.semantic import mint_cube_jwt  # noqa: E402


async def _fetch_cube_meta(workspace) -> dict:
    """Fetch raw Cube /v1/meta JSON for the given workspace.

    Mints a short-lived JWT scoped to the workspace's schema and calls the
    Cube REST API, mirroring the pattern used by ``semantic_catalog``.
    """
    schema_name = _ws_hash(workspace)
    token = mint_cube_jwt(str(workspace.id), schema_name)
    meta_url = f"{settings.CUBE_REST_URL.rstrip('/')}/v1/meta"
    async with httpx.AsyncClient() as client:
        response = await client.get(
            meta_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


async def ensure_measure_queryable_meta(
    workspace, measure_name: str, *, timeout_s: float = 15, interval_s: float = 0.5
) -> bool:
    """Poll Cube /v1/meta until *measure_name* appears in the blended cube.

    Cube recompiles models asynchronously on file change (dev-mode hot-reload).
    This polls until ``BLENDED_CUBE.<measure_name>`` is visible or the deadline
    is exceeded.

    Args:
        workspace: Workspace ORM instance (provides id + schema_name via ``_ws_hash``).
        measure_name: Unqualified measure name, e.g. ``"kmc_hours"``.
        timeout_s: Total seconds to wait before giving up (default 15).
        interval_s: Seconds between poll attempts (default 0.5).

    Returns:
        ``True`` if the measure became queryable within the timeout, else ``False``.
    """
    target = f"{BLENDED_CUBE}.{measure_name}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        meta = await _fetch_cube_meta(workspace)
        for cube in meta.get("cubes", []):
            if cube.get("name") == BLENDED_CUBE and any(
                m.get("name") == target for m in cube.get("measures", [])
            ):
                return True
        await asyncio.sleep(interval_s)
    return False

LABS_PROVIDER = "commcare_connect_labs"


def _stg_visits_columns(schema_name: str) -> list[str]:
    """The physical columns of an opp's stg_visits (system-minted schema name)."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'stg_visits' ORDER BY column_name",
            [schema_name],
        )
        return [r[0] for r in cur.fetchall()]


def _derived_column_candidates(schema_name: str, form_columns: set[str]) -> list[FieldCandidate]:
    """Offer staging-DERIVED stg_visits columns (not backed by a form question) to the
    resolver. Form fields cover entered values (e.g. visit weight); derived columns like
    ``child_age`` (age in days, computed by staging) are real, resolvable fields too — so
    per-visit concepts that aren't form entries can still align across opps."""
    return [
        FieldCandidate(
            path=f"/stg_visits/{col}", json_path="", column=col,
            label=col.replace("_", " "), type="DerivedColumn",
            form_name="stg_visits", module_name="derived", case_type="visit", is_entry=True,
        )
        for col in _stg_visits_columns(schema_name)
        if col not in form_columns
    ]


def _attach_samples(candidates, schema_name: str):
    """Attach a few real per-column sample values so the resolver can spot which columns
    hold real data vs placeholder garbage (e.g. child_age '17.881' vs child_age_2 'sample-101').

    Samples a bounded window and keeps the first few distinct non-empty values per column."""
    wanted = {c.column for c in candidates}
    with connection.cursor() as cur:
        # Random window, not the first N rows: clinical fields (e.g. child_age) are sparse —
        # the leading rows can be all learn/quiz visits, hiding the real values.
        cur.execute(
            f'SELECT * FROM "{schema_name}".stg_visits ORDER BY random() LIMIT 800'  # noqa: S608 system-minted schema
        )
        names = [d[0] for d in cur.description]
        rows = cur.fetchall()
    samples: dict[str, list[str]] = {}
    for idx, name in enumerate(names):
        if name not in wanted:
            continue
        seen: list[str] = []
        for r in rows:
            v = r[idx]
            if v is None:
                continue
            s = str(v).strip()
            if s and s not in seen:
                seen.append(s)
            if len(seen) >= 4:
                break
        samples[name] = seen
    return [dataclasses.replace(c, samples=tuple(samples.get(c.column, ()))) for c in candidates]


_PLACEHOLDER_RE = re.compile(r"^sample[-_]\d+$", re.IGNORECASE)


def _drop_placeholder_columns(candidates):
    """Drop columns whose sampled values are ALL synthetic placeholders (e.g. 'sample-101').

    These never hold a real measure, and removing them both de-noises the resolver shortlist
    and shrinks the prompt (the synthetic seed has hundreds of such filler columns). Columns
    with no samples are kept — they may be sparse-but-real (resolved by label)."""
    return [
        c
        for c in candidates
        if not (c.samples and all(_PLACEHOLDER_RE.match(v) for v in c.samples))
    ]


def workspace_opps(workspace):
    """Active opps in the workspace + their stg_visits field candidates (sync ORM).

    Candidates = form-question fields PLUS the staging-derived stg_visits columns, each
    carrying real sample values, so the resolver can align both entered and computed fields
    (e.g. age_days -> child_age, not the placeholder child_age_2) across heterogeneous apps."""
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
        form_cands = gather_measure_candidates(form_defs)
        # Gate derived candidates on form ENTRY columns only: a calculated form field
        # (is_entry=False, e.g. child_age) is dropped by the clinical-entry shortlist, so we
        # must re-offer its physical column as a derived (is_entry=True) candidate or the
        # resolver never sees the real value.
        derived = _derived_column_candidates(
            schema.schema_name, {c.column for c in form_cands if c.is_entry}
        )
        opps.append(OppRef(tenant.external_id, schema.schema_name))
        sampled = _attach_samples(form_cands + derived, schema.schema_name)
        cands[tenant.external_id] = _drop_placeholder_columns(sampled)
    return opps, cands


async def resolve_across_opps_from_candidates(spec, candidates_by_opp, *, model_client=None):
    """Resolve the measure for every opp CONCURRENTLY.

    The per-opp LLM calls are independent, so we fan them out with asyncio.gather
    instead of awaiting them one-by-one. Sequential resolution over ~11 opps took
    ~50s — long enough that the chat stream gave up before the tool returned.
    Concurrent resolution collapses that to roughly one call's latency.
    """
    opp_ids = list(candidates_by_opp)
    results = await asyncio.gather(
        *(resolve_measure(spec, candidates_by_opp[o], model_client=model_client) for o in opp_ids)
    )
    return dict(zip(opp_ids, results, strict=True))


def shortlist_for_opp(candidates) -> list[dict]:
    return [
        {"column": c.column, "label": c.label, "type": c.type}
        for c in _clinical_entry_candidates(candidates)
    ]
