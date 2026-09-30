"""Calculated dimensions keep row grain, workspace scope, and SQL safety."""

import pytest

from apps.semantic.services.field_sql import (
    DimensionSQLValidationError,
    JoinSQLValidationError,
    MeasureSQLValidationError,
    compile_dimension_sql,
    compile_join_sql,
    compile_measure_filter_sql,
    compile_measure_sql,
)
from mcp_server.services.sql_validator import DANGEROUS_FUNCTIONS


@pytest.mark.parametrize("source", ["content", '{CUBE}."content"', "{CUBE}.content"])
def test_dimension_sql_compiles_scoped_columns(source):
    result = compile_dimension_sql(
        f"CASE WHEN {source} ILIKE '%update%' THEN 'Account' ELSE 'Other' END",
        columns={"content"},
    )
    assert result == "CASE WHEN {CUBE}.\"content\" ILIKE '%update%' THEN 'Account' ELSE 'Other' END"


def test_dimension_sql_preserves_quoted_identifiers_and_pins_functions():
    result = compile_dimension_sql('lower("Display Name")', columns={"Display Name"})
    assert result == 'pg_catalog.LOWER({CUBE}."Display Name")'


def test_dimension_sql_normalizes_unquoted_identifiers():
    assert compile_dimension_sql("CONTENT", columns={"content"}) == '{CUBE}."content"'
    with pytest.raises(DimensionSQLValidationError, match="not a column"):
        compile_dimension_sql('"CONTENT"', columns={"content"})


@pytest.mark.parametrize(
    "source,expected",
    [
        ("'{CUBE}'", r"'\u007bCUBE\u007d'"),
        ("$txt${CUBE}$txt$", r"'\u007bCUBE\u007d'"),
        ("'{does_not_exist}'", r"'\u007bdoes_not_exist\u007d'"),
        ("'{{already_doubled}}'", r"'\u007b\u007balready_doubled\u007d\u007d'"),
        ("'{'", r"'\u007b'"),
        ("'}'", r"'\u007d'"),
        ('\'{"topic": "Account"}\'', '\'\\u007b"topic": "Account"\\u007d\''),
        ('{CUBE}."content" /* {does_not_exist} */', '{CUBE}."content"'),
        ("content -- {does_not_exist}", '{CUBE}."content"'),
        ("/* {does_not_exist} */ content", '{CUBE}."content"'),
        ('{CUBE}."column{CUBE}"', r'{CUBE}."column\u007bCUBE\u007d"'),
        ('"column{does_not_exist}"', r'{CUBE}."column\u007bdoes_not_exist\u007d"'),
        (
            "concat(content, '{CUBE}')",
            "pg_catalog.CONCAT({CUBE}.\"content\", '\\u007bCUBE\\u007d')",
        ),
        (r"'\u007b'", r"'\\u007b'"),
        ("'line\nbreak {CUBE}'", r"'line\nbreak \u007bCUBE\u007d'"),
        ("'{% invalid_jinja %}'", r"'\u007b% invalid_jinja %\u007d'"),
    ],
)
def test_dimension_sql_escapes_cube_literal_braces_and_removes_comments(source, expected):
    assert (
        compile_dimension_sql(source, columns={"content", "column{CUBE}", "column{does_not_exist}"})
        == expected
    )


@pytest.mark.parametrize(
    "source",
    [
        "count(*)",
        "SUM(amount)",
        "pg_catalog.sum(amount)",
        "avg(amount) OVER ()",
        "row_number()",
        "row_number() OVER ()",
        "unnest(ARRAY[1, 2])",
        "generate_series(1, 3)",
        "jsonb_array_elements('[1,2]'::jsonb)",
        "regexp_split_to_table(content, ',')",
        "(SELECT amount FROM raw_visits)",
        "amount FROM raw_visits",
        "amount WHERE true",
        "DISTINCT amount",
        "amount AS alias",
        "amount, content",
        "*",
        "amount; SELECT 1",
        "amount; DROP TABLE raw_visits",
        "pg_sleep(1)",
        "pg_read_file('/etc/passwd')",
        "set_config('search_path', 'public', true)",
        "custom_function(content)",
        "public.lower(content)",
        "amount::regclass",
        'other_dataset."amount"',
        'public.raw_visits."amount"',
        "not_a_column",
        "{count}",
        "{raw_visits.amount}",
        "{CUBE}.not_a_column",
        "{CUBE}.",
        "{CUBE",
        "$1",
        ":parameter",
        "",
        "a" * 1001,
    ],
)
def test_dimension_sql_rejects_unsafe_or_non_scalar_expressions(source):
    with pytest.raises(DimensionSQLValidationError):
        compile_dimension_sql(source, columns={"amount", "content"})


@pytest.mark.parametrize(
    "source",
    [
        "coalesce(content, 'Unknown')",
        "amount * 1.5",
        "CASE WHEN amount > 0 THEN true ELSE false END",
        "date_trunc('month', created_at)",
        "created_at::date",
        "btrim(content)",
        "regexp_replace(content, 'account', 'profile', 'gi')",
        "(metadata ->> 'topic')",
    ],
)
def test_dimension_sql_accepts_scalar_analytics(source):
    assert compile_dimension_sql(source, columns={"amount", "content", "created_at", "metadata"})


# Every function the shared validator denies, and any statement appended after an
# expression, must be rejected by each SQL-bearing member property.
MEMBER_COLUMNS = {
    "amount",
    "data",
    "flagged",
    "name",
    "paid",
    "status",
    "topic",
    "user_id",
    "visit_date",
}

DENIED_SQL = [
    *(f"max({name}('x'))" for name in sorted(DANGEROUS_FUNCTIONS)),
    *(f"pg_catalog.{name}('x')" for name in sorted(DANGEROUS_FUNCTIONS)),
    "current_setting('work_mem')",
    "{CUBE}.amount; RESET ALL",
    "{CUBE}.amount; SELECT 1",
    "count(*); COPY raw_visits TO STDOUT",
    "(SELECT pg_sleep(1))",
    "sum((SELECT 1))",
    "public.sum({CUBE}.amount)",
    "{CUBE}.amount::regclass",
]


@pytest.mark.parametrize("source", DENIED_SQL)
def test_measure_sql_rejects_denied_sql(source):
    with pytest.raises(MeasureSQLValidationError):
        compile_measure_sql(source, columns=MEMBER_COLUMNS)


@pytest.mark.parametrize("source", DENIED_SQL)
def test_measure_filter_sql_rejects_denied_sql(source):
    with pytest.raises(MeasureSQLValidationError):
        compile_measure_filter_sql(source, columns=MEMBER_COLUMNS)


# Join conditions may use an unqualified IN (SELECT ...), so the harmless scalar
# subquery case is not a rejection there.
@pytest.mark.parametrize("source", [s for s in DENIED_SQL if s != "sum((SELECT 1))"])
def test_join_sql_rejects_denied_sql(source):
    with pytest.raises(JoinSQLValidationError):
        compile_join_sql(f"{{visits.user_id}} = {{users.id}} AND {source} IS NOT NULL")


@pytest.mark.parametrize(
    "source,expected",
    [
        ("{CUBE}.amount", "{CUBE}.amount"),
        ('{CUBE}."amount"', '{CUBE}."amount"'),
        ("{CUBE.opportunity_id}", "{CUBE.opportunity_id}"),
        ("count(*)", "pg_catalog.COUNT(*)"),
        ("sum({CUBE}.amount)", "pg_catalog.SUM({CUBE}.amount)"),
        ("avg({amount})", "pg_catalog.AVG({amount})"),
        ("count(DISTINCT {CUBE}.user_id)", "pg_catalog.COUNT(DISTINCT {CUBE}.user_id)"),
        (
            "{approved_count}::numeric / NULLIF({raw_visits.count}, 0)",
            "CAST({approved_count} AS DECIMAL) / NULLIF({raw_visits.count}, 0)",
        ),
        (
            "coalesce(sum(CASE WHEN {status} = 'approved' THEN 1 ELSE 0 END), 0)",
            "COALESCE(pg_catalog.SUM(CASE WHEN {status} = 'approved' THEN 1 ELSE 0 END), 0)",
        ),
        (
            "date_trunc('month', max({CUBE}.visit_date))",
            "pg_catalog.date_trunc('month', pg_catalog.MAX({CUBE}.visit_date))",
        ),
        (
            "count(*) FILTER (WHERE {CUBE}.amount > 0)",
            "pg_catalog.COUNT(*) FILTER(WHERE {CUBE}.amount > 0)",
        ),
        (
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY {CUBE}.amount)",
            "pg_catalog.PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {CUBE}.amount)",
        ),
        ("round(avg({CUBE}.amount)::numeric, 2)", None),
        ("stddev({CUBE}.amount)", None),
        ("bool_or({CUBE}.flagged)", None),
        ("string_agg({CUBE}.name, ', ')", None),
        ("{CUBE}.amount -- trailing comment", "{CUBE}.amount"),
    ],
)
def test_measure_sql_accepts_aggregate_expressions(source, expected):
    result = compile_measure_sql(source, columns=MEMBER_COLUMNS)
    if expected is not None:
        assert result == expected


@pytest.mark.parametrize(
    "source",
    [
        "sum({CUBE}.amount) OVER ()",
        "(SELECT count(*) FROM raw_visits)",
        "{CUBE}.amount AS alias",
        "sum({CUBE}.amount) FROM raw_visits",
        "*",
        "custom_function({CUBE}.amount)",
        "generate_series(1, 3)",
        "unnest(ARRAY[1, 2])",
        "{CUBE}.a.b",
        "public.raw_visits.amount",
        "{a.b.c}",
        "{count()}",
        "{CUBE}x",
        "{CUBE",
        "1}",
        "__scout_ref_0",
        "$1",
        "",
        "a" * 1001,
    ],
)
def test_measure_sql_rejects_non_expression_sql(source):
    with pytest.raises(MeasureSQLValidationError):
        compile_measure_sql(source, columns=MEMBER_COLUMNS)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("{CUBE}.\"status\" = 'approved'", "{CUBE}.\"status\" = 'approved'"),
        ("{status} IN ('a', 'b')", "{status} IN ('a', 'b')"),
        ("{CUBE}.amount > 0 AND {CUBE}.paid IS NOT NULL", None),
        ("lower({CUBE}.name) LIKE 'a%'", None),
        ("{CUBE}.topic ~ '[0-9]{2}'", None),
        ("coalesce({CUBE}.amount, 0) > 10", None),
    ],
)
def test_measure_filter_sql_accepts_row_conditions(source, expected):
    result = compile_measure_filter_sql(source, columns=MEMBER_COLUMNS)
    if expected is not None:
        assert result == expected


@pytest.mark.parametrize("source", ["sum({CUBE}.amount) > 0", "count(*) > 1"])
def test_measure_filter_sql_rejects_aggregates(source):
    with pytest.raises(MeasureSQLValidationError, match="row-level"):
        compile_measure_filter_sql(source, columns=MEMBER_COLUMNS)


def test_join_sql_accepts_generated_identity_joins():
    source = (
        "{visits.user_id} = {users.id} AND ({users.id}) IN "
        '(SELECT "id" FROM "raw_users" GROUP BY "id" HAVING COUNT(*) = 1) '
        "AND {visits.user_id} <> ''"
    )
    assert compile_join_sql(source) == (
        "{visits.user_id} = {users.id} AND ({users.id}) IN "
        '(SELECT "id" FROM "raw_users" GROUP BY "id" HAVING pg_catalog.COUNT(*) = 1) '
        "AND {visits.user_id} <> ''"
    )


@pytest.mark.parametrize(
    "source",
    [
        "{visits.user_id} IN (SELECT id FROM public.raw_users)",
        "{visits.user_id} IN (SELECT oid FROM pg_class)",
        "",
    ],
)
def test_join_sql_rejects_other_schemas_and_catalogs(source):
    with pytest.raises(JoinSQLValidationError):
        compile_join_sql(source)


@pytest.mark.parametrize(
    "source",
    [
        "{CUBE}.not_a_column",
        'sum({CUBE}."AMOUNT")',
        "count(DISTINCT not_a_column)",
        "raw_visits.amount",
        "{users}.amount",
        "{CUBE.count}.amount",
    ],
)
def test_measure_sql_only_qualifies_real_dataset_columns(source):
    with pytest.raises(MeasureSQLValidationError):
        compile_measure_sql(source, columns=MEMBER_COLUMNS)
    with pytest.raises(MeasureSQLValidationError):
        compile_measure_filter_sql(f"{source} IS NOT NULL", columns=MEMBER_COLUMNS)


def test_measure_sql_normalizes_unquoted_columns_like_postgres():
    assert compile_measure_sql("sum({CUBE}.AMOUNT)", columns=MEMBER_COLUMNS) == (
        "pg_catalog.SUM({CUBE}.AMOUNT)"
    )


@pytest.mark.parametrize(
    "source",
    ["{CUBE}.data ? 'a'", "{CUBE}.data ?| ARRAY['a', 'b']", "{CUBE}.data ?& ARRAY['a']"],
)
def test_measure_filter_sql_accepts_jsonb_key_operators(source):
    assert compile_measure_filter_sql(source, columns=MEMBER_COLUMNS)


def test_join_sql_qualifies_only_owning_dataset_columns():
    assert compile_join_sql('{CUBE}."user_id" = {users.id}', columns={"user_id"}) == (
        '{CUBE}."user_id" = {users.id}'
    )
    for source in (
        "{CUBE}.not_a_column = {users.id}",
        "{CUBE}.user_id = {users.id}",
        "{visits}.user_id = {users.id}",
        "{visits.user_id} IN (SELECT raw_users.id FROM raw_users)",
    ):
        with pytest.raises(JoinSQLValidationError):
            compile_join_sql(source, columns={"user_id"} if "not_a" in source else None)
