"""The SQL contract for semantic-model SQL fragments that reach Cube.

``expression`` remains a physical column name. ``metadata.cube_sql`` is an
explicit SQL calculation; dimensions may use a scalar expression over columns
of their own dataset, but cannot introduce joins, aggregates, or a new grain.
Measure SQL, measure filters and join conditions go through the same shared
validator. Canvas diagnostics and Cube schema generation use these compilers.
"""

from __future__ import annotations

import re

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
        "pg_typeof",
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


_AGGREGATE_FUNCTIONS = frozenset(
    {
        "array_agg",
        "avg",
        "bool_and",
        "bool_or",
        "corr",
        "count",
        "covar_pop",
        "covar_samp",
        "group_concat",
        "j_s_o_n_array_agg",
        "j_s_o_n_b_object_agg",
        "j_s_o_n_object_agg",
        "json_agg",
        "json_object_agg",
        "jsonb_agg",
        "jsonb_object_agg",
        "logical_and",
        "logical_or",
        "max",
        "min",
        "percentile_cont",
        "percentile_disc",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "string_agg",
        "sum",
        "var_pop",
        "var_samp",
        "variance",
        "variance_pop",
    }
)
# JSONB key/path operators the shared validator already allows in queries.
_ROW_FUNCTIONS = _SCALAR_FUNCTIONS | {
    "j_s_o_n_b_contains_all_top_keys",
    "j_s_o_n_b_contains_any_top_keys",
    "j_s_o_n_b_contains_top_key",
    "j_s_o_n_b_path_exists",
}
_MEASURE_FUNCTIONS = _ROW_FUNCTIONS | _AGGREGATE_FUNCTIONS
_MAX_MEMBER_SQL_LENGTH = 1000
_REFERENCE_PLACEHOLDER = "__scout_ref_"
_REFERENCE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")


class SemanticSQLValidationError(ValueError):
    """A semantic-model SQL fragment is not safe to publish to Cube."""


class DimensionSQLValidationError(SemanticSQLValidationError):
    """A calculated dimension is not a safe, dataset-local scalar expression."""


class MeasureSQLValidationError(SemanticSQLValidationError):
    """A measure's SQL or filter is not a safe expression."""


class JoinSQLValidationError(SemanticSQLValidationError):
    """A relationship's join condition is not a safe expression."""


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


def compile_measure_sql(value: str, *, columns: set[str]) -> str:
    """Validate a measure's SQL and return it with its Cube references restored.

    Aggregates are allowed; subqueries, windows and functions outside the
    allowlist are not. The caller still embeds the result with ``embed_cube_sql``
    against the cube's known references.
    """
    return _compile_member_sql(
        value,
        label="measure SQL",
        error=MeasureSQLValidationError,
        functions=_MEASURE_FUNCTIONS,
        allow_aggregates=True,
        columns=columns,
    )


def compile_measure_filter_sql(value: str, *, columns: set[str]) -> str:
    """Validate a measure filter as a row-level boolean expression."""
    return _compile_member_sql(
        value,
        label="measure filter SQL",
        error=MeasureSQLValidationError,
        functions=_ROW_FUNCTIONS,
        allow_aggregates=False,
        columns=columns,
    )


def compile_join_sql(value: str, *, columns: set[str] | None = None) -> str:
    """Validate a relationship's join condition.

    ``columns`` are the owning (from) dataset's columns, which ``{CUBE}.column``
    may name. Generated identity joins filter the target through ``IN (SELECT ...
    GROUP BY ... HAVING COUNT(*) = 1)``, so unqualified subqueries stay legal
    here; the shared validator still bounds their functions and tables.
    """
    return _compile_member_sql(
        value,
        label="join SQL",
        error=JoinSQLValidationError,
        functions=None,
        allow_aggregates=True,
        allow_subqueries=True,
        max_length=None,
        columns=columns,
        check_bare_columns=False,
    )


def _compile_member_sql(
    value: str,
    *,
    label: str,
    error: type[SemanticSQLValidationError],
    functions: frozenset[str] | None,
    allow_aggregates: bool,
    columns: set[str] | None,
    allow_subqueries: bool = False,
    check_bare_columns: bool = True,
    max_length: int | None = _MAX_MEMBER_SQL_LENGTH,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"The {label} must be a nonempty string.")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise error(f"The {label} must be at most {max_length} characters.")
    if _REFERENCE_PLACEHOLDER in value.lower():
        raise error(f"The {label} cannot use the reserved name '{_REFERENCE_PLACEHOLDER}'.")
    text, references = _replace_member_references(value, label=label, error=error)
    try:
        statement = SQLValidator().validate(f"SELECT {text}")
    except (SQLValidationError, TokenError) as exc:
        raise error(f"Invalid {label}: {exc}") from exc
    if (
        not isinstance(statement, exp.Select)
        or len(statement.expressions) != 1
        or any(value for key, value in statement.args.items() if key != "expressions")
    ):
        raise error(f"The {label} must be one SQL expression, not a SELECT query or SQL clauses.")
    expression = statement.expressions[0]
    if expression.find(exp.Alias, exp.Window) is not None:
        raise error(f"The {label} cannot use aliases or window functions.")
    if not allow_subqueries and expression.find(exp.Query) is not None:
        raise error(f"The {label} cannot contain a subquery; use a custom dataset instead.")
    if not allow_aggregates and expression.find(exp.AggFunc) is not None:
        raise error(f"The {label} must be a row-level condition without aggregates.")
    for star in expression.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count) and not _inside_subquery(star, expression):
            raise error(f"The {label} can only use * inside count(*).")
    for table in expression.find_all(exp.Table):
        if table.db or table.catalog:
            raise error(f"The {label} cannot reference schema-qualified tables.")
    if functions is not None:
        for function in expression.find_all(exp.Func):
            name = (
                function.name if isinstance(function, exp.Anonymous) else function.sql_name()
            ).lower()
            if name not in functions:
                raise error(f"Function '{name}' is not supported in {label}.")
    for column in list(expression.find_all(exp.Column)):
        if column.args.get("db") or column.args.get("catalog"):
            raise error(f"The {label} cannot use schema- or database-qualified columns.")
        # PostgreSQL reads rel.name as name(rel) when rel has no such column, so a
        # qualified name must be a real column of this dataset, never an arbitrary word.
        if column.table in references:
            if references[column.table] != "CUBE" or columns is None:
                raise _qualified_column_error(label, error, columns)
            _require_dataset_column(column, columns, label=label, error=error)
            column.set("table", exp.Var(this="{CUBE}"))
        elif column.table:
            raise _qualified_column_error(label, error, columns)
        elif column.name in references:
            reference = exp.Var(this="{" + references[column.name] + "}")
            if column is expression:
                expression = reference
            else:
                column.replace(reference)
        elif (
            check_bare_columns and columns is not None and not _inside_subquery(column, expression)
        ):
            _require_dataset_column(column, columns, label=label, error=error)
    sql = expression.sql(dialect="postgres", comments=False)
    # Anything left over sat somewhere other than a value or column qualifier.
    if _REFERENCE_PLACEHOLDER in sql:
        raise error(f"The {label} can only use Cube references as values or column qualifiers.")
    return sql


def _qualified_column_error(
    label: str, error: type[SemanticSQLValidationError], columns: set[str] | None
) -> SemanticSQLValidationError:
    if columns is None:
        return error(
            f"The {label} can only qualify columns as {{CUBE}}.column of the owning "
            "dataset; write other members as {dataset.field}."
        )
    return error(
        f"The {label} can only qualify columns as {{CUBE}}.column; "
        "reference other members as {member}."
    )


def _require_dataset_column(
    column: exp.Column,
    columns: set[str],
    *,
    label: str,
    error: type[SemanticSQLValidationError],
) -> None:
    identifier = column.this
    if not isinstance(identifier, exp.Identifier):
        raise error(f"The {label} can only reference named columns of this dataset.")
    name = identifier.name if identifier.quoted else identifier.name.lower()
    if name not in columns:
        raise error(
            f"'{name}' is not a column on this dataset. Use {{CUBE}}.\"column\" for "
            "a physical column or {member} for another dimension or measure."
        )


def _inside_subquery(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not root and node.parent is not None:
        node = node.parent
        if node is not root and isinstance(node, exp.Query):
            return True
    return False


def _replace_member_references(
    value: str, *, label: str, error: type[SemanticSQLValidationError]
) -> tuple[str, dict[str, str]]:
    """Swap ``{member}`` references for identifiers the SQL parser accepts."""
    try:
        tokens = Postgres().tokenize(value)
    except TokenError as exc:
        raise error(f"Invalid {label}: {exc}") from exc
    references: dict[str, str] = {}
    parts: list[str] = []
    position = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.token_type == TokenType.L_BRACE:
            end = index + 1
            while end < len(tokens) and tokens[end].token_type != TokenType.R_BRACE:
                end += 1
            if end >= len(tokens):
                raise error(f"The {label} has an unterminated Cube reference.")
            reference = value[token.end + 1 : tokens[end].start].strip()
            if not _REFERENCE_RE.fullmatch(reference):
                raise error(f"The {label} has an invalid Cube reference {{{reference[:100]}}}.")
            placeholder = f"{_REFERENCE_PLACEHOLDER}{len(references)}"
            references[placeholder] = reference
            parts.extend((value[position : token.start], placeholder))
            position = tokens[end].end + 1
            index = end
        elif token.token_type == TokenType.R_BRACE:
            raise error(f"The {label} has an unmatched closing brace.")
        index += 1
    parts.append(value[position:])
    return "".join(parts), references
