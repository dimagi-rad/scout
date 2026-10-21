import json
import re

import pytest
from django.db import connection

from apps.transformations.services.commcare_staging import (
    _form_xmlns,
    _generate_case_type_asset,
    _generate_form_asset,
)
from apps.transformations.services.connect_staging import _generate_stg_visits
from apps.transformations.services.repeat_identity import _canonical_case, _parent_source
from apps.transformations.services.staging_source import detoasted_source
from apps.users.models import Tenant

_WRAPPER = re.compile(
    r"\nFROM \(\n    SELECT .*\n    FROM (\w+)\n(?:    WHERE (.*)\n)?    OFFSET 0\n\) AS \1\Z"
)


def _plain(sql):
    """The pre-#918 shape of a generated model: the same projection over a plain scan."""
    match = _WRAPPER.search(sql)
    assert match, sql[-300:]
    where = f"\nWHERE {match[2]}" if match[2] else ""
    return f"{sql[: match.start()]}\nFROM {match[1]}{where}"


def _questions(count):
    return [{"value": f"/data/q{i}", "type": "Int" if i % 2 else "Text"} for i in range(count)]


@pytest.fixture
def tenant():
    return Tenant(provider="commcare", external_id="detoast")


@pytest.mark.parametrize("count", [3, 1700], ids=["plain", "folded"])
def test_generated_connect_parent_keeps_its_plain_source_identity(tenant, count):
    sql = _generate_stg_visits(tenant, {"du": {"questions": _questions(count)}}).sql_content

    identity = _parent_source(sql, provider="commcare_connect")
    assert identity is not None
    assert identity == _parent_source(_plain(sql), provider="commcare_connect")


@pytest.mark.parametrize("count", [3, 1700], ids=["plain", "folded"])
def test_generated_form_keeps_its_plain_source_identity(tenant, count):
    sql = _generate_form_asset(
        tenant, "urn:it's", {"questions": _questions(count)}, model_name="form_x"
    ).sql_content

    identity = _parent_source(sql, provider="commcare")
    assert identity is not None
    assert identity == _parent_source(_plain(sql), provider="commcare")
    assert _form_xmlns(sql) == _form_xmlns(_plain(sql)) == "urn:it's"


@pytest.mark.parametrize("count", [3, 1700], ids=["plain", "folded"])
def test_generated_case_stays_canonical(tenant, count):
    properties = [f"p{i}" for i in range(count)]
    sql = _generate_case_type_asset(
        tenant, "patient", properties, {}, model_name="patient"
    ).sql_content

    assert _canonical_case(_plain(sql))
    assert _canonical_case(sql)
    assert not _canonical_case(sql.replace("    OFFSET 0\n", "    OFFSET 1\n"))


_PLAIN_VISITS = (
    "SELECT\n    visit_id,\n    opportunity_id,\n    username,\n    entity_id,\n    status,\n"
    "    deliver_unit_id,\n    form_json,\n"
    "    form_json #>> ARRAY['data','muac']::text[] AS \"muac\"\n"
    "FROM raw_visits"
)
_DETOASTED_VISITS = _PLAIN_VISITS.replace(
    "FROM raw_visits",
    detoasted_source(
        "raw_visits",
        ["visit_id", "opportunity_id", "username", "entity_id", "status", "deliver_unit_id"],
        "form_json",
    ),
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


@pytest.mark.parametrize(
    "inner",
    [
        "SELECT * FROM raw_forms",
        "SELECT a, count(*) FROM raw_forms",
        'SELECT a, "my col" FROM raw_forms',
        "SELECT form_json #> '{}' AS form_json FROM raw_forms",
        "SELECT a, b.form_json #> '{}' AS form_json FROM raw_forms b",
    ],
)
def test_other_subquery_sources_are_not_canonical_and_do_not_raise(inner):
    sql = f"SELECT x FROM ({inner}) AS raw_forms WHERE xmlns = 'a'"
    assert _parent_source(sql, provider="commcare") is None
    assert _parent_source(sql, provider="commcare_connect") is None
    assert not _canonical_case(sql)


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
