"""Calculated dimensions keep row grain, workspace scope, and SQL safety."""

import pytest

from apps.semantic.services.field_sql import DimensionSQLValidationError, compile_dimension_sql


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


def test_dimension_sql_does_not_rewrite_cube_text_inside_sql_literals():
    assert compile_dimension_sql("'{CUBE}'", columns=set()) == "'{CUBE}'"
    assert compile_dimension_sql("$txt${CUBE}$txt$", columns=set()) == "'{CUBE}'"


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
