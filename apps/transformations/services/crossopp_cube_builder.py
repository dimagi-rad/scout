"""Assemble a workspace's cross-opp Cube model from per-opp measure resolutions.

Two tiers (Cube Data Blending):

- **Tier 1** — one cube per opp (``opp_<id>``): an aligned ``SELECT`` over that opp's
  ``stg_visits`` that aliases each resolved expression to the shared canonical column name
  (``NULL`` where a measure is absent in that app). This is the durable, reusable unit.
- **Tier 2** — a blended cube that ``UNION ALL``s the per-opp cubes via ``{opp_<id>.sql()}``,
  stamps a constant ``opportunity_id``, and defines the shared measures **once**.

All cubes are emitted into ONE model file so they compile in the same context (required for
``{cube.sql()}`` references to resolve). No new Django models — this is Cube YAML, served by
the existing per-schema ``repositoryFactory`` path.
"""

# ruff: noqa: S608 — this module composes Cube *model* SQL strings (written to YAML and run by
# Cube read-only under the workspace's least-privilege role), not runtime parameterized queries.

from __future__ import annotations

from dataclasses import dataclass

import yaml

from apps.transformations.services.measure_resolver import (
    CanonicalMeasureSpec,
    MeasureResolution,
)

# Always-present visit columns carried through from stg_visits (dimensions on the blend).
_BASE_COLUMNS = ["visit_id", "visit_date", "status", "username"]
_BASE_DIMENSIONS = [
    ("visit_date", "time"),
    ("status", "string"),
    ("username", "string"),
]


@dataclass(frozen=True)
class OppRef:
    """One opportunity participating in the workspace blend."""

    external_id: str  # the Connect-Labs opp id, e.g. "10012" — also the opportunity_id value
    schema_name: str  # its tenant schema, e.g. "t_10012_62a6d140"


def opp_cube_name(external_id: str) -> str:
    return f"opp_{external_id}"


# Matches any string PostgreSQL can parse as a number (mirrors commcare_staging's guard).
_NUM_RE = r"^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$"


def _safe_numeric(column: str) -> str:
    """Regex-guarded numeric cast so synthetic placeholder text (e.g. "sample-223") becomes
    NULL instead of raising a cast error — stg_visits columns may be text."""
    return f"CASE WHEN NULLIF({column}::text, '') ~ '{_NUM_RE}' THEN ({column})::numeric ELSE NULL END"


def _measure_select(measure: CanonicalMeasureSpec, resolution: MeasureResolution | None) -> str:
    """The per-opp SELECT term aliasing a measure to its canonical column.

    Absent → a TYPED ``NULL::numeric`` (so the blended avg ignores that opp). The cast is
    required: an untyped ``NULL`` is ``unknown`` and a ``UNION ALL`` of an untyped-NULL branch
    with a numeric branch fails ("UNION types text and numeric cannot be matched"). Both
    measure kinds land on numeric below (safe-cast value, or 0.0/1.0 rate), so an absent term
    must be ``numeric`` too for every opp to align. A ``numeric`` measure is safe-cast from its
    resolved column (placeholders → NULL, never a hard cast error); a ``rate`` measure's
    boolean expression becomes a 0.0/1.0 numeric the blended cube averages.
    """
    if resolution is None or resolution.status == "absent" or not resolution.sql_expression:
        return f"NULL::numeric AS {measure.name}"
    if measure.kind == "rate":
        return f"CASE WHEN {resolution.sql_expression} THEN 1.0 ELSE 0.0 END AS {measure.name}"
    column = resolution.column or resolution.sql_expression
    return f"{_safe_numeric(column)} AS {measure.name}"


def build_opp_cube(
    opp: OppRef,
    measures: list[CanonicalMeasureSpec],
    resolutions: dict[str, MeasureResolution],
) -> dict:
    """Tier-1: the aligned per-opp cube (a SELECT over that opp's stg_visits)."""
    terms = list(_BASE_COLUMNS)
    terms += [_measure_select(m, resolutions.get(m.name)) for m in measures]
    # This composes a Cube *model* SQL string (written to YAML, executed by Cube), not a
    # runtime parameterized query. schema_name is system-minted; the LLM-resolved expressions
    # run read-only under the workspace's least-privilege role (see isolation hardening).
    sql = f"SELECT {', '.join(terms)}\nFROM {opp.schema_name}.stg_visits"
    return {"name": opp_cube_name(opp.external_id), "sql": sql}


def build_blended_cube(
    name: str,
    opps: list[OppRef],
    measures: list[CanonicalMeasureSpec],
) -> dict:
    """Tier-2: union the per-opp cubes, stamp opportunity_id, define measures once."""
    branches = [
        f"SELECT '{opp.external_id}' AS opportunity_id, b.*\n  FROM {{{opp_cube_name(opp.external_id)}.sql()}} AS b"
        for opp in opps
    ]
    sql = "\nUNION ALL\n".join(branches)

    dimensions = [{"name": "opportunity_id", "sql": "opportunity_id", "type": "string"}]
    dimensions += [{"name": n, "sql": n, "type": t} for n, t in _BASE_DIMENSIONS]

    measure_defs: list[dict] = [{"name": "visits", "type": "count"}]
    for m in measures:
        # Tier-1 already produced a numeric (value, or 0.0/1.0 for a rate) → average it.
        measure_defs.append({"name": m.name, "sql": m.name, "type": "avg"})

    return {"name": name, "sql": sql, "dimensions": dimensions, "measures": measure_defs}


def render_crossopp_model(
    blended_name: str,
    opps: list[OppRef],
    measures: list[CanonicalMeasureSpec],
    resolutions_by_opp: dict[str, dict[str, MeasureResolution]],
) -> str:
    """Render the full workspace model (per-opp cubes + blended cube) as one YAML string.

    ``resolutions_by_opp`` is keyed by ``OppRef.external_id`` → {measure_name → resolution}.
    """
    cubes = [
        build_opp_cube(opp, measures, resolutions_by_opp.get(opp.external_id, {}))
        for opp in opps
    ]
    cubes.append(build_blended_cube(blended_name, opps, measures))
    return yaml.safe_dump({"cubes": cubes}, sort_keys=False, width=1000)
