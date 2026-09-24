"""The SQL contract for row-level, calculated semantic dimensions.

``expression`` remains a physical column name. ``metadata.cube_sql`` is an
explicit SQL calculation; dimensions may use a scalar expression over columns
of their own dataset, but cannot introduce joins, aggregates, or a new grain.
The same compiler is used by canvas diagnostics and Cube schema generation.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.tokens import TokenType

from apps.semantic.services.cube_sql import embed_cube_sql
from mcp_server.services.sql_validator import SQLValidationError, SQLValidator

_ROW_ALIAS = "__scout_dimension_row"
# A positive scalar subset of the shared validator's analytics functions. A
# function becoming available to full SELECTs must not silently make it legal
# in a grouping dimension (aggregates/SRFs may be safe SQL but change grain).
_SCALAR_FUNCTIONS = frozenset(
    {
        "abs",
        "age",
        "and",
        "array",
        "array_length",
        "array_size",
        "array_to_string",
        "array_contains_all",
        "array_contained_by",
        "array_overlaps",
        "btrim",
        "cardinality",
        "case",
        "cast",
        "ceil",
        "ceiling",
        "char_length",
        "character_length",
        "coalesce",
        "concat",
        "concat_ws",
        "current_date",
        "current_time",
        "current_timestamp",
        "date_part",
        "date_trunc",
        "exp",
        "extract",
        "floor",
        "greatest",
        "if",
        "json_array_length",
        "json_build_array",
        "json_build_object",
        "json_extract",
        "json_extract_scalar",
        "jsonb_array_length",
        "jsonb_build_array",
        "jsonb_build_object",
        "jsonb_extract",
        "jsonb_extract_scalar",
        "jsonb_extract_path",
        "jsonb_extract_path_text",
        "jsonb_typeof",
        "least",
        "left",
        "length",
        "ln",
        "localtime",
        "log",
        "lower",
        "lpad",
        "ltrim",
        "md5",
        "mod",
        "now",
        "nullif",
        "or",
        "pad",
        "position",
        "pg_input_is_valid",
        "pow",
        "power",
        "regexp_extract",
        "regexp_i_like",
        "regexp_like",
        "regexp_replace",
        "regexp_split_to_array",
        "replace",
        "right",
        "round",
        "rpad",
        "rtrim",
        "sign",
        "split_part",
        "sqrt",
        "str_position",
        "str_to_date",
        "str_to_time",
        "substring",
        "time_to_str",
        "to_char",
        "to_date",
        "to_json",
        "to_jsonb",
        "to_number",
        "to_timestamp",
        "translate",
        "trim",
        "trunc",
        "unix_to_time",
        "upper",
        "width_bucket",
    }
)


class DimensionSQLValidationError(ValueError):
    """A calculated dimension is not a safe, dataset-local scalar expression."""


def dataset_column_names(dataset) -> set[str]:
    """Physical columns, excluding the expressions of calculated fields."""
    columns: set[str] = set()
    for field in dataset.fields.all():
        metadata = field.metadata or {}
        source_column = metadata.get("source_column")
        if source_column:
            columns.add(source_column)
        elif not metadata.get("cube_sql") and field.expression and field.expression != "*":
            columns.add(field.expression)
    return columns


def compile_dimension_sql(value: str, *, columns: set[str]) -> str:
    """Validate a scalar expression and return safe Cube/PostgreSQL SQL.

    Accept both ``{CUBE}."column"`` and bare column references. All columns are
    validated against this dataset and emitted as quoted Cube references. The
    existing SQL validator supplies the read-only/function/type boundary and
    pins allowed functions to pg_catalog; authored SQL is never executed here.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise DimensionSQLValidationError(
            "Dimension sql must be a nonempty string of at most 1000 characters."
        )
    try:
        statement = SQLValidator().validate(f"SELECT {_replace_cube_reference(value)}")
    except (SQLValidationError, TokenError) as exc:
        raise DimensionSQLValidationError(f"Invalid dimension SQL: {exc}") from exc
    if (
        not isinstance(statement, exp.Select)
        or len(statement.expressions) != 1
        or any(value for key, value in statement.args.items() if key != "expressions")
    ):
        raise DimensionSQLValidationError(
            "Use one scalar SQL expression, not a SELECT query or SQL clauses."
        )
    expression = statement.expressions[0]
    if expression.find(exp.Query, exp.AggFunc, exp.Window, exp.Star, exp.Alias) is not None:
        raise DimensionSQLValidationError(
            "Dimension SQL must preserve row grain. Use a measure for aggregates or a custom dataset for queries/windows."
        )
    for function in expression.find_all(exp.Func):
        name = (
            function.name if isinstance(function, exp.Anonymous) else function.sql_name()
        ).lower()
        if name not in _SCALAR_FUNCTIONS:
            raise DimensionSQLValidationError(
                f"Function '{name}' is not supported in a row-level dimension; "
                "use a measure for aggregates or a custom dataset for row-changing logic."
            )
    normalize_identifiers(expression, dialect="postgres")
    for column in expression.find_all(exp.Column):
        if column.db or column.catalog or column.table not in {"", _ROW_ALIAS}:
            raise DimensionSQLValidationError(
                'Dimension SQL can only reference this dataset\'s columns, using {CUBE}."column" or a bare column name.'
            )
        if column.name not in columns:
            raise DimensionSQLValidationError(
                f"'{column.name}' is not a column on this dataset. Inspect the dataset's columns before writing dimension SQL."
            )
        column.set("this", exp.to_identifier(column.name, quoted=True))
        column.set("table", exp.Var(this="{CUBE}"))
    return embed_cube_sql(expression.sql(dialect="postgres", comments=False), references={"CUBE"})


def _replace_cube_reference(value: str) -> str:
    """Replace Cube tokens, leaving quoted strings/identifiers untouched."""
    tokens = Postgres().tokenize(value)
    replacements: list[tuple[int, int]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.token_type == TokenType.L_BRACE:
            if (
                index + 2 >= len(tokens)
                or tokens[index + 1].text != "CUBE"
                or tokens[index + 2].token_type != TokenType.R_BRACE
            ):
                raise DimensionSQLValidationError(
                    'Calculated dimensions use {CUBE}."column", not measure/member references.'
                )
            replacements.append((token.start, tokens[index + 2].end + 1))
            index += 2
        elif token.token_type == TokenType.R_BRACE:
            raise DimensionSQLValidationError("Invalid Cube reference in dimension SQL.")
        index += 1
    for start, end in reversed(replacements):
        value = value[:start] + _ROW_ALIAS + value[end:]
    return value
