import json

import pytest
from django.db import connection

from apps.transformations.services.commcare_staging import _form_xmlns
from apps.transformations.services.repeat_identity import _canonical_case, _parent_source
from apps.transformations.services.staging_source import detoasted_source

_VISIT_COLUMNS = ["visit_id", "opportunity_id", "username", "entity_id", "status"]
_VISIT_SELECT = (
    "SELECT\n    visit_id,\n    opportunity_id,\n    username,\n    entity_id,\n    status,\n"
    "    deliver_unit_id,\n    form_json,\n"
    "    form_json #>> ARRAY['data','muac']::text[] AS \"muac\"\n"
)
_PLAIN_VISITS = _VISIT_SELECT + "FROM raw_visits"
_DETOASTED_VISITS = _VISIT_SELECT + detoasted_source(
    "raw_visits", [*_VISIT_COLUMNS, "deliver_unit_id"], "form_json"
)


def test_detoasted_connect_parent_keeps_its_plain_source_identity():
    assert _parent_source(_PLAIN_VISITS, provider="commcare_connect") is not None
    assert _parent_source(_DETOASTED_VISITS, provider="commcare_connect") == _parent_source(
        _PLAIN_VISITS, provider="commcare_connect"
    )


def test_detoasted_form_and_case_keep_their_plain_source_identity():
    form_select = (
        'SELECT form_id, xmlns, received_on::timestamp AS "received_on", app_id, form_data, '
        "form_data #>> ARRAY['data','q']::text[] AS \"q\"\n"
    )
    plain = form_select + "FROM raw_forms\nWHERE xmlns = 'urn:it''s'"
    detoasted = form_select + detoasted_source(
        "raw_forms",
        ["form_id", "xmlns", "received_on", "app_id"],
        "form_data",
        where="xmlns = 'urn:it''s'",
    )
    assert _parent_source(detoasted, provider="commcare") == _parent_source(
        plain, provider="commcare"
    )
    assert _form_xmlns(plain) == _form_xmlns(detoasted) == "urn:it's"

    case_select = (
        "SELECT case_id, case_type, case_name, owner_id, "
        'date_opened::timestamp AS "date_opened", last_modified::timestamp AS "last_modified", '
        "closed, properties->>'dob' AS \"dob\"\n"
    )
    assert _canonical_case(
        case_select
        + detoasted_source(
            "raw_cases",
            [
                "case_id",
                "case_type",
                "case_name",
                "owner_id",
                "date_opened",
                "last_modified",
                "closed",
            ],
            "properties",
            where="case_type = 'patient'",
        )
    )


@pytest.mark.parametrize(
    "tampered",
    [
        _DETOASTED_VISITS.replace("form_json #> '{}' AS form_json", "'{}'::jsonb AS form_json"),
        _DETOASTED_VISITS.replace("    OFFSET 0\n", "    OFFSET 1\n"),
        _DETOASTED_VISITS.replace("    FROM raw_visits", "    FROM other_table"),
        _DETOASTED_VISITS.replace("SELECT visit_id,", "SELECT lower(visit_id) AS visit_id,"),
        _DETOASTED_VISITS.replace(
            "    OFFSET 0\n", "    WHERE status = 'approved'\n    OFFSET 0\n"
        ),
        _DETOASTED_VISITS + "\nWHERE status = 'approved'",
    ],
)
def test_only_an_exact_detoasted_wrapper_reads_as_the_plain_source(tampered):
    assert _parent_source(tampered, provider="commcare_connect") is None


@pytest.mark.django_db
def test_detoasted_source_returns_every_json_value_unchanged():
    documents = [
        {"data": {"muac": "12.5", "big": "x" * 5000}},
        [1, 2],
        "text",
        3,
        None,  # JSON null
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE raw_visits (visit_id int, status text, form_json jsonb) "
            "ON COMMIT DROP"
        )
        cursor.executemany(
            "INSERT INTO raw_visits VALUES (%s, 'ok', %s::jsonb)",
            [(i, json.dumps(doc)) for i, doc in enumerate(documents)],
        )
        cursor.execute("INSERT INTO raw_visits VALUES (99, 'ok', NULL)")
        select = "SELECT visit_id, status, form_json, form_json #>> ARRAY['data','muac'] AS muac\n"
        cursor.execute(select + "FROM raw_visits ORDER BY visit_id")
        plain = cursor.fetchall()
        cursor.execute(
            f"SELECT * FROM ({select}"
            f"{detoasted_source('raw_visits', ['visit_id', 'status'], 'form_json')}) s "
            "ORDER BY visit_id"
        )
        assert cursor.fetchall() == plain
        cursor.execute(
            f"SELECT count(*) FROM ({select}"
            f"{detoasted_source('raw_visits', ['visit_id', 'status'], 'form_json')}) s "
            "WHERE form_json IS NOT NULL AND form_json = 'null'::jsonb"
        )
        assert cursor.fetchone() == (1,)
