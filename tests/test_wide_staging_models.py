"""Generated staging models must fit PostgreSQL's column limits (#712).

A SELECT may name at most 1664 target entries and a table at most 1600 columns.
Connect's ``stg_visits`` flattens every deliver-app question into a column, so a
large app failed the whole system stage.
"""

from __future__ import annotations

import logging
import re

import pytest
import sqlglot

from apps.transformations.services.commcare_staging import (
    MAX_STAGING_COLUMNS,
    POSTGRES_MAX_TABLE_COLUMNS,
    generate_system_assets,
)
from apps.transformations.services.connect_staging import (
    generate_connect_assets,
    visit_column_map,
)
from apps.transformations.services.repeat_identity import RepeatSource, repeat_source
from apps.users.models import Tenant

POSTGRES_TARGET_LIST_LIMIT = 1664
WIDE = 2100
VISIT_BASE = [
    "visit_id",
    "opportunity_id",
    "username",
    "entity_id",
    "status",
    "deliver_unit_id",
    "form_json",
]
FORM_BASE = ["form_id", "xmlns", "received_on", "app_id", "form_data"]
CASE_BASE = [
    "case_id",
    "case_type",
    "case_name",
    "owner_id",
    "date_opened",
    "last_modified",
    "closed",
]
_REF = re.compile(r"\{\{\s*ref\('([a-z0-9_]+)'\)\s*\}\}")


def _questions(count, *, prefix="/data/q", qtype="Text", repeat=None):
    questions = []
    for index in range(count):
        question = {"label": f"Q{index}", "value": f"{prefix}{index:04d}", "type": qtype}
        if repeat is not None:
            question["repeat"] = repeat
        questions.append(question)
    return questions


def _columns(sql):
    tree = sqlglot.parse_one(_REF.sub(r"\1", sql), read="postgres")
    return [projection.alias_or_name for projection in tree.expressions]


def _by_name(assets):
    return {asset.name: asset for asset in assets}


@pytest.fixture
def connect_tenant():
    return Tenant(provider="commcare_connect", external_id="synthetic-wide-connect")


@pytest.fixture
def commcare_tenant():
    return Tenant(provider="commcare", external_id="synthetic-wide-commcare")


def test_wide_visit_form_fits_postgres_limits_and_keeps_raw_json(connect_tenant):
    form_defs = {"visit": {"name": "Visit", "questions": _questions(WIDE)}}

    sql = _by_name(generate_connect_assets(form_defs, connect_tenant))["stg_visits"].sql_content
    columns = _columns(sql)

    assert len(columns) <= MAX_STAGING_COLUMNS < POSTGRES_MAX_TABLE_COLUMNS
    assert len(columns) < POSTGRES_TARGET_LIST_LIMIT
    # Folded answers stay queryable through the raw JSON column.
    assert columns[: len(VISIT_BASE)] == VISIT_BASE
    kept = MAX_STAGING_COLUMNS - len(VISIT_BASE)
    assert columns[len(VISIT_BASE) :] == [f"q{index:04d}" for index in range(kept)]


def test_visit_column_map_matches_the_folded_model(connect_tenant):
    form_defs = {"visit": {"name": "Visit", "questions": _questions(WIDE)}}

    sql = _by_name(generate_connect_assets(form_defs, connect_tenant))["stg_visits"].sql_content

    # Column notes are keyed by these names; a folded field must not get a note.
    assert [alias for _, alias in visit_column_map(form_defs)] == _columns(sql)[len(VISIT_BASE) :]


def test_repeated_sources_and_labels_fold_before_answers(connect_tenant):
    shared = _questions(1000, prefix="/data/shared/q")
    form_defs = {
        "first": {
            "name": "First",
            "questions": shared + _questions(200, prefix="/data/label", qtype="Trigger"),
        },
        "second": {
            "name": "Second",
            "questions": [dict(q) for q in shared] + _questions(300, prefix="/data/second/r"),
        },
    }

    sql = _by_name(generate_connect_assets(form_defs, connect_tenant))["stg_visits"].sql_content
    columns = _columns(sql)[len(VISIT_BASE) :]

    labels = MAX_STAGING_COLUMNS - len(VISIT_BASE) - 1300
    assert columns == (
        [f"q{index:04d}" for index in range(1000)]
        + [f"label{index:04d}" for index in range(labels)]
        + [f"r{index:04d}" for index in range(300)]
    )
    # The second form's copies of shared paths hold the same data, so only they fold.
    assert "ARRAY['data','second','r0000']" in sql
    assert sql.count("ARRAY['data','shared','q0000']") == 1


def test_wide_form_case_and_repeat_models_all_fit(commcare_tenant):
    metadata = {
        "case_types": [{"name": "patient"}],
        "app_definitions": [
            {
                "modules": [
                    {
                        "case_type": "patient",
                        "case_properties": [f"x{index:04d}" for index in range(WIDE)]
                        + ["properties"],
                    }
                ]
            }
        ],
        "form_definitions": {
            "urn:synthetic:wide": {
                "name": "Wide",
                "questions": _questions(WIDE)
                + _questions(WIDE, prefix="/data/items/c", repeat="/data/items"),
            }
        },
    }

    assets = _by_name(generate_system_assets(commcare_tenant, metadata))
    form = _columns(assets["stg_form_wide"].sql_content)
    case = _columns(assets["stg_case_patient"].sql_content)
    repeat = _columns(assets["stg_form_wide__repeat_items"].sql_content)

    for columns in (form, case, repeat):
        assert len(columns) <= MAX_STAGING_COLUMNS
    assert form[: len(FORM_BASE)] == FORM_BASE
    # Case models have no raw JSON column of their own until they fold.
    assert case[: len(CASE_BASE) + 1] == [*CASE_BASE, "properties"]
    assert "properties_2" in case
    assert repeat[:3] == ["form_id", "repeat_index", "repeat_data"]


def _generate(tenant, form_defs, existing=None):
    if tenant.provider == "commcare":
        return generate_system_assets(
            tenant, {"form_definitions": form_defs}, existing_assets=existing
        )
    return generate_connect_assets(form_defs, tenant, existing_assets=existing)


@pytest.mark.parametrize("provider", ["commcare", "commcare_connect"])
def test_folded_repeat_keeps_its_source_identity(provider):
    tenant = Tenant(provider=provider, external_id="synthetic-wide-repeat")
    repeat_flag = "/data/items" if provider == "commcare" else True
    form_defs = {
        "urn:synthetic:wide": {
            "name": "Wide",
            "questions": _questions(WIDE, prefix="/data/items/c", repeat=repeat_flag),
        }
    }

    first = _generate(tenant, form_defs)
    parent, repeat = first

    assert repeat_source(repeat.sql_content, provider=provider) == RepeatSource(
        parent.name, ("data", "items")
    )
    # Regenerating over the saved models must not demand a migration or rename anything.
    second = _generate(tenant, form_defs, existing=first)
    assert [(a.name, a.sql_content) for a in second] == [(a.name, a.sql_content) for a in first]


def test_folded_repeat_is_a_safe_orphan(commcare_tenant):
    form_defs = {
        "urn:synthetic:wide": {
            "name": "Wide",
            "questions": _questions(WIDE, prefix="/data/items/c", repeat="/data/items"),
        }
    }
    old = _generate(commcare_tenant, form_defs)

    assert _generate(commcare_tenant, {}, existing=old) == []


def test_folded_case_model_is_a_safe_orphan(commcare_tenant):
    metadata = {
        "case_types": [{"name": "patient"}],
        "app_definitions": [
            {
                "modules": [
                    {
                        "case_type": "patient",
                        "case_properties": [f"prop{index:04d}" for index in range(WIDE)],
                    }
                ]
            }
        ],
    }
    old = generate_system_assets(commcare_tenant, metadata)

    assert generate_system_assets(commcare_tenant, {}, existing_assets=old) == []


def test_generation_is_deterministic(connect_tenant, commcare_tenant):
    shared = _questions(900, prefix="/data/shared/q")
    form_defs = {
        "first": {"name": "First", "questions": shared + _questions(900, prefix="/data/a/q")},
        "second": {"name": "Second", "questions": shared + _questions(900, prefix="/data/b/q")},
    }

    def snapshot(assets):
        return [(asset.name, asset.sql_content) for asset in assets]

    connect = snapshot(generate_connect_assets(form_defs, connect_tenant))
    commcare = snapshot(generate_system_assets(commcare_tenant, {"form_definitions": form_defs}))

    for _ in range(3):
        assert snapshot(generate_connect_assets(form_defs, connect_tenant)) == connect
        assert (
            snapshot(generate_system_assets(commcare_tenant, {"form_definitions": form_defs}))
            == commcare
        )


def test_small_visit_model_is_unchanged(connect_tenant):
    form_defs = {
        "visit": {
            "name": "Visit",
            "questions": [
                {"value": "/data/muac", "type": "Decimal"},
                {"value": "/data/status", "type": "Text"},
                {"value": "/data/label", "type": "Trigger"},
            ],
        }
    }

    sql = _by_name(generate_connect_assets(form_defs, connect_tenant))["stg_visits"].sql_content

    assert sql == (
        "SELECT\n"
        "    visit_id,\n"
        "    opportunity_id,\n"
        "    username,\n"
        "    entity_id,\n"
        "    status,\n"
        "    deliver_unit_id,\n"
        "    form_json,\n"
        "    NULLIF(form_json #>> ARRAY['data','muac']::text[], '')::numeric AS \"muac\",\n"
        "    form_json #>> ARRAY['data','status']::text[] AS \"status_2\",\n"
        "    form_json #>> ARRAY['data','label']::text[] AS \"label\"\n"
        "FROM raw_visits"
    )


def _case_metadata(count):
    return {
        "case_types": [{"name": "patient"}],
        "app_definitions": [
            {
                "modules": [
                    {
                        "case_type": "patient",
                        "case_properties": [f"prop{index:04d}" for index in range(count)],
                    }
                ]
            }
        ],
    }


def test_models_that_fit_a_table_are_not_folded(connect_tenant, commcare_tenant, caplog):
    # A model with 1501-1600 columns builds on main; folding it would drop columns
    # that dependent models and saved queries may already use.
    form_defs = {
        "visit": {
            "name": "Visit",
            "questions": _questions(POSTGRES_MAX_TABLE_COLUMNS - len(VISIT_BASE)),
        }
    }
    case_metadata = _case_metadata(POSTGRES_MAX_TABLE_COLUMNS - len(CASE_BASE))

    with caplog.at_level(logging.WARNING):
        visits = _by_name(generate_connect_assets(form_defs, connect_tenant))["stg_visits"]
        case = _by_name(generate_system_assets(commcare_tenant, case_metadata))["stg_case_patient"]

    assert len(_columns(visits.sql_content)) == POSTGRES_MAX_TABLE_COLUMNS
    case_columns = _columns(case.sql_content)
    assert len(case_columns) == POSTGRES_MAX_TABLE_COLUMNS
    assert "properties" not in case_columns
    assert not caplog.records


def test_one_column_past_the_table_limit_folds_to_the_budget(connect_tenant, commcare_tenant):
    form_defs = {
        "visit": {
            "name": "Visit",
            "questions": _questions(POSTGRES_MAX_TABLE_COLUMNS - len(VISIT_BASE) + 1),
        }
    }
    case_metadata = _case_metadata(POSTGRES_MAX_TABLE_COLUMNS - len(CASE_BASE) + 1)

    visits = _by_name(generate_connect_assets(form_defs, connect_tenant))["stg_visits"]
    case = _by_name(generate_system_assets(commcare_tenant, case_metadata))["stg_case_patient"]

    assert len(_columns(visits.sql_content)) == MAX_STAGING_COLUMNS
    case_columns = _columns(case.sql_content)
    assert len(case_columns) == MAX_STAGING_COLUMNS
    assert case_columns[len(CASE_BASE)] == "properties"


def test_folding_logs_a_warning_with_the_folded_count(connect_tenant, caplog):
    form_defs = {"visit": {"name": "Visit", "questions": _questions(WIDE)}}

    with caplog.at_level(logging.INFO):
        generate_connect_assets(form_defs, connect_tenant)

    folded = [r for r in caplog.records if "folded" in r.getMessage()]
    assert len(folded) == 1
    assert folded[0].levelno == logging.WARNING
    kept = MAX_STAGING_COLUMNS - len(VISIT_BASE)
    assert f"folded {WIDE - kept} of {WIDE} fields into form_json" in folded[0].getMessage()
    assert "stg_visits" in folded[0].getMessage()
