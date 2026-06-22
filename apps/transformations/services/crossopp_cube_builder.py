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


# Canonical PER-VISIT fields (resolved per opp, like measures) that power per-visit
# cross-opp analysis such as growth curves. The resolver aligns each to whatever column
# the opp's app actually uses (e.g. visit_weight -> child_weight_visit in most opps but
# child_weight in opp 10020), so heterogeneous apps line up on one canonical name.
VISIT_FIELDS = ["visit_weight", "age_days"]
# The per-child key carried through so distinct-infant counts work across opps.
_CHILD_KEY_COLUMN = "entity_id"

# Birth-weight bands (grams) for the growth-curve dimension. References the canonical
# ``birth_weight`` measure column produced by Tier-1, so it only renders when the
# workspace has a birth_weight measure resolved.
_BIRTHWEIGHT_BAND_SQL = (
    "CASE "
    "WHEN birth_weight >= 1000 AND birth_weight < 1250 THEN '1000-1250' "
    "WHEN birth_weight >= 1250 AND birth_weight < 1500 THEN '1250-1500' "
    "WHEN birth_weight >= 1500 AND birth_weight < 1750 THEN '1500-1750' "
    "WHEN birth_weight >= 1750 AND birth_weight < 2000 THEN '1750-2000' "
    "ELSE NULL END"
)


def _visit_field_select(name: str, resolution: MeasureResolution | None) -> str:
    """Per-opp SELECT term aliasing a per-visit field to its canonical column.

    Absent → ``NULL::numeric`` so a UNION ALL branch from an opp lacking the field
    still type-aligns with opps that have it (same rule as ``_measure_select``)."""
    if resolution is None or resolution.status == "absent" or not (
        resolution.column or resolution.sql_expression
    ):
        return f"NULL::numeric AS {name}"
    column = resolution.column or resolution.sql_expression
    return f"{_safe_numeric(column)} AS {name}"


def build_opp_cube(
    opp: OppRef,
    measures: list[CanonicalMeasureSpec],
    resolutions: dict[str, MeasureResolution],
    visit_resolutions: dict[str, MeasureResolution] | None = None,
) -> dict:
    """Tier-1: the aligned per-opp cube (a SELECT over that opp's stg_visits).

    When ``visit_resolutions`` is given, the cube also carries the per-visit canonical
    fields (visit_weight, age_days) + the child key — each aligned to this opp's own
    columns — so the blend can expose per-visit dimensions/measures."""
    terms = list(_BASE_COLUMNS)
    terms += [_measure_select(m, resolutions.get(m.name)) for m in measures]
    if visit_resolutions is not None:
        terms.append(f"{_CHILD_KEY_COLUMN} AS child_id")
        terms += [_visit_field_select(f, visit_resolutions.get(f)) for f in VISIT_FIELDS]
    # This composes a Cube *model* SQL string (written to YAML, executed by Cube), not a
    # runtime parameterized query. schema_name is system-minted; the LLM-resolved expressions
    # run read-only under the workspace's least-privilege role (see isolation hardening).
    sql = f"SELECT {', '.join(terms)}\nFROM {opp.schema_name}.stg_visits"
    return {"name": opp_cube_name(opp.external_id), "sql": sql}


def build_blended_cube(
    name: str,
    opps: list[OppRef],
    measures: list[CanonicalMeasureSpec],
    *,
    with_visit_fields: bool = False,
) -> dict:
    """Tier-2: union the per-opp cubes, stamp opportunity_id, define measures once.

    ``with_visit_fields`` adds the per-visit growth surface: ``age_days`` +
    ``birthweight_band`` dimensions and visit-weight measures (avg + a Cube-native 95%
    CI computed from sum / sum-of-squares / count, since Cube has no stddev type)."""
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

    if with_visit_fields:
        dimensions.append({"name": "age_days", "sql": "floor(age_days)", "type": "number"})
        # Weekly bin: per-day points are sparse/noisy for a growth curve; grouping by
        # age_week gives a smooth, well-powered line (more children per point).
        dimensions.append({"name": "age_week", "sql": "floor(age_days / 7)", "type": "number"})
        # The band needs the birth_weight measure column; only emit it when resolved.
        if any(m.name == "birth_weight" for m in measures):
            dimensions.append(
                {"name": "birthweight_band", "sql": _BIRTHWEIGHT_BAND_SQL, "type": "string"}
            )
        measure_defs += [
            {"name": "avg_visit_weight", "sql": "visit_weight", "type": "avg"},
            # weighted N = count of non-null visit_weight (avg ignores NULLs, so N must too).
            {
                "name": "weighed_visits",
                "sql": "CASE WHEN visit_weight IS NOT NULL THEN 1 ELSE 0 END",
                "type": "sum",
            },
            {"name": "children", "sql": "child_id", "type": "count_distinct"},
            # Internal sums for the CI; SUM ignores NULLs so they pair with weighed_visits.
            {"name": "_sum_visit_weight", "sql": "visit_weight", "type": "sum"},
            {
                "name": "_sumsq_visit_weight",
                "sql": "visit_weight * visit_weight",
                "type": "sum",
            },
            # 95% CI half-width = 1.96 * sample_stddev / sqrt(N), with
            # variance = (Σx² - (Σx)²/N) / (N-1). Cube ``number`` measures reference
            # other measures via {name}, so the cube returns the CI directly.
            {
                "name": "ci95_visit_weight",
                "type": "number",
                "sql": (
                    "CASE WHEN {weighed_visits} > 1 THEN "
                    "1.96 * sqrt(GREATEST(({_sumsq_visit_weight} - {_sum_visit_weight} * "
                    "{_sum_visit_weight} / NULLIF({weighed_visits}, 0)) / "
                    "NULLIF({weighed_visits} - 1, 0), 0) / NULLIF({weighed_visits}, 0)) "
                    "ELSE 0 END"
                ),
            },
        ]

    return {"name": name, "sql": sql, "dimensions": dimensions, "measures": measure_defs}


def render_crossopp_model(
    blended_name: str,
    opps: list[OppRef],
    measures: list[CanonicalMeasureSpec],
    resolutions_by_opp: dict[str, dict[str, MeasureResolution]],
    visit_resolutions_by_opp: dict[str, dict[str, MeasureResolution]] | None = None,
) -> str:
    """Render the full workspace model (per-opp cubes + blended cube) as one YAML string.

    ``resolutions_by_opp`` is keyed by ``OppRef.external_id`` → {measure_name → resolution}.
    ``visit_resolutions_by_opp`` (same shape, keyed by per-visit field name) adds the
    per-visit growth surface — present only once the workspace has resolved visit fields.
    """
    with_visit = visit_resolutions_by_opp is not None
    cubes = [
        build_opp_cube(
            opp,
            measures,
            resolutions_by_opp.get(opp.external_id, {}),
            (visit_resolutions_by_opp or {}).get(opp.external_id, {}) if with_visit else None,
        )
        for opp in opps
    ]
    cubes.append(build_blended_cube(blended_name, opps, measures, with_visit_fields=with_visit))
    return yaml.safe_dump({"cubes": cubes}, sort_keys=False, width=1000)
