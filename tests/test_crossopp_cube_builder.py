"""Tests for the cross-opp Cube model assembler (per-opp cubes + blended Data Blending cube)."""

from __future__ import annotations

import yaml

from apps.transformations.services.crossopp_cube_builder import (
    OppRef,
    build_blended_cube,
    build_opp_cube,
    opp_cube_name,
    render_crossopp_model,
)
from apps.transformations.services.measure_resolver import (
    CanonicalMeasureSpec,
    MeasureResolution,
)

MEASURES = [
    CanonicalMeasureSpec("birth_weight", "newborn weight in grams", "numeric"),
    CanonicalMeasureSpec("danger_sign_referral_rate", "danger sign + referral", "rate"),
]


def _res(measure, column, expr, status="resolved"):
    return MeasureResolution(
        measure=measure,
        column=column,
        source_path=f"/data/{column}",
        sql_expression=expr,
        confidence=0.9,
        status=status,
        matched_label=column,
        reason="x",
    )


def test_opp_cube_aliases_resolved_expressions():
    opp = OppRef("10012", "t_10012_62a6d140")
    res = {
        "birth_weight": _res("birth_weight", "child_weight_birth", "CAST(child_weight_birth AS NUMERIC)"),
        "danger_sign_referral_rate": _res(
            "danger_sign_referral_rate", "child_referred", "(child_referred = 'yes')"
        ),
    }
    cube = build_opp_cube(opp, MEASURES, res)
    assert cube["name"] == "opp_10012"
    assert "FROM t_10012_62a6d140.stg_visits" in cube["sql"]
    # numeric measure: safe regex-guarded cast on the resolved column (placeholders -> NULL)
    assert "(child_weight_birth)::numeric" in cube["sql"]
    assert "AS birth_weight" in cube["sql"]
    # rate measure wrapped to 0.0/1.0 numeric
    assert "CASE WHEN (child_referred = 'yes') THEN 1.0 ELSE 0.0 END AS danger_sign_referral_rate" in cube["sql"]


def test_opp_cube_emits_null_for_absent_measure():
    opp = OppRef("10020", "t_10020_250f6746")
    res = {
        "birth_weight": _res("birth_weight", None, None, status="absent"),
        "danger_sign_referral_rate": _res(
            "danger_sign_referral_rate", "danger_signs", "(danger_signs <> '')"
        ),
    }
    cube = build_opp_cube(opp, MEASURES, res)
    assert "NULL AS birth_weight" in cube["sql"]
    assert "AS danger_sign_referral_rate" in cube["sql"]


def test_blended_cube_unions_opp_cubes_with_opportunity_id():
    opps = [OppRef("10012", "t_10012_x"), OppRef("10020", "t_10020_y")]
    blend = build_blended_cube("kmc_cross_opp", opps, MEASURES)
    sql = blend["sql"]
    assert "UNION ALL" in sql
    # references the Tier-1 cubes via {cube.sql()} and stamps the opportunity_id constant
    assert "{opp_10012.sql()}" in sql
    assert "{opp_10020.sql()}" in sql
    assert "'10012' AS opportunity_id" in sql
    assert "'10020' AS opportunity_id" in sql
    dim_names = {d["name"] for d in blend["dimensions"]}
    assert "opportunity_id" in dim_names
    meas = {m["name"]: m for m in blend["measures"]}
    assert meas["visits"]["type"] == "count"
    assert meas["birth_weight"]["type"] == "avg"
    assert meas["danger_sign_referral_rate"]["type"] == "avg"


def test_render_model_is_valid_yaml_with_all_cubes():
    opps = [OppRef("10012", "t_10012_x"), OppRef("10020", "t_10020_y")]
    res_by_opp = {
        "10012": {
            "birth_weight": _res("birth_weight", "child_weight_birth", "child_weight_birth"),
            "danger_sign_referral_rate": _res(
                "danger_sign_referral_rate", "child_referred", "(child_referred = 'yes')"
            ),
        },
        "10020": {
            "birth_weight": _res("birth_weight", None, None, status="absent"),
            "danger_sign_referral_rate": _res(
                "danger_sign_referral_rate", "danger_signs", "(danger_signs <> '')"
            ),
        },
    }
    text = render_crossopp_model("kmc_cross_opp", opps, MEASURES, res_by_opp)
    model = yaml.safe_load(text)  # must round-trip as valid YAML
    names = [c["name"] for c in model["cubes"]]
    assert names == [opp_cube_name("10012"), opp_cube_name("10020"), "kmc_cross_opp"]
    blended = model["cubes"][-1]
    assert "{opp_10012.sql()}" in blended["sql"]
