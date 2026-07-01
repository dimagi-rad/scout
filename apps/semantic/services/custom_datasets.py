"""Validation and column inference for workspace-authored custom datasets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import psycopg
from asgiref.sync import async_to_sync
from psycopg import sql as psql

from mcp_server.context import load_workspace_context


class CustomDatasetError(ValueError):
    """Raised when a custom dataset cannot be compiled safely."""


@dataclass(frozen=True)
class InferredColumn:
    name: str
    data_type: str


_IDENT = r'(?:"[^"]+"|[a-zA-Z_][a-zA-Z0-9_]*)'
_TABLE_REF_RE = re.compile(rf"\b(from|join)\s+({_IDENT}(?:\s*\.\s*{_IDENT})?)", re.IGNORECASE)
_CTE_RE = re.compile(rf"(?:\bwith|,)\s+({_IDENT})\s+as\s*\(", re.IGNORECASE)
_DENIED_SQL_RE = re.compile(
    r"\b("
    r"alter|analyze|call|copy|create|delete|do|drop|execute|grant|insert|merge|"
    r"refresh|reindex|revoke|truncate|update|vacuum"
    r")\b",
    re.IGNORECASE,
)
_SYSTEM_CATALOG_RE = re.compile(
    r"\b(information_schema|pg_catalog|pg_class|pg_namespace|pg_tables|pg_views)\b",
    re.IGNORECASE,
)


def compile_custom_dataset_sql(
    definition_sql: str,
    *,
    schema_name: str,
    allowed_tables: dict[str, str],
) -> str:
    """Return a workspace-scoped SELECT statement for a custom dataset.

    ``allowed_tables`` maps lower-case semantic dataset names and physical table
    names to the real physical table/view name in the workspace query schema.
    """
    sql = _normalize_single_statement(definition_sql)
    lowered = sql.lstrip().lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise CustomDatasetError("Custom datasets must be defined by a SELECT or WITH query.")
    if _DENIED_SQL_RE.search(sql):
        raise CustomDatasetError("Custom dataset SQL must be read-only.")
    if _SYSTEM_CATALOG_RE.search(sql):
        raise CustomDatasetError("Custom dataset SQL cannot reference system catalogs.")

    cte_names = {_normalize_identifier(match.group(1)) for match in _CTE_RE.finditer(sql)}

    def replace_table(match: re.Match[str]) -> str:
        keyword = match.group(1)
        table_ref = match.group(2)
        if "." in table_ref:
            raise CustomDatasetError("Custom dataset SQL cannot use explicit schema references.")
        table_key = _normalize_identifier(table_ref)
        if table_key in cte_names:
            return match.group(0)
        table_name = allowed_tables.get(table_key)
        if not table_name:
            raise CustomDatasetError(
                f"Custom dataset SQL references unknown workspace table '{table_key}'."
            )
        return f"{keyword} {_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"

    return _TABLE_REF_RE.sub(replace_table, sql)


def infer_custom_dataset_columns(workspace, compiled_sql: str) -> list[dict[str, Any]]:
    """Probe a compiled custom dataset query and return catalog-style columns."""
    ctx = async_to_sync(load_workspace_context)(str(workspace.id))
    try:
        with psycopg.connect(**ctx.connection_params, autocommit=True) as conn, conn.cursor() as cursor:
            cursor.execute(psql.SQL("SET ROLE {}").format(psql.Identifier(ctx.readonly_role)))
            try:
                cursor.execute(
                    psql.SQL("SET search_path TO {}, public").format(
                        psql.Identifier(ctx.schema_name)
                    )
                )
                cursor.execute("SET statement_timeout TO '30s'")
                cursor.execute(
                    psql.SQL("SELECT * FROM ({}) AS scout_custom_dataset_probe LIMIT 0").format(
                        psql.SQL(compiled_sql)
                    )
                )
                if not cursor.description:
                    raise CustomDatasetError("Custom dataset query did not return columns.")
                description = list(cursor.description)
                type_oids = sorted({desc.type_code for desc in description if desc.type_code})
                type_names: dict[int, str] = {}
                if type_oids:
                    cursor.execute(
                        "SELECT oid::int, format_type(oid, NULL) FROM pg_type WHERE oid = ANY(%s)",
                        (type_oids,),
                    )
                    type_names = {int(oid): name for oid, name in cursor.fetchall()}
                return [
                    {
                        "name": desc.name,
                        "type": type_names.get(int(desc.type_code), ""),
                        "nullable": None,
                    }
                    for desc in description
                ]
            finally:
                cursor.execute("RESET ROLE")
    except CustomDatasetError:
        raise
    except Exception as exc:
        raise CustomDatasetError(f"Custom dataset SQL failed validation: {exc}") from exc


def _normalize_single_statement(sql: str) -> str:
    value = (sql or "").strip()
    if not value:
        raise CustomDatasetError("Custom dataset SQL is required.")
    if value.endswith(";"):
        value = value[:-1].strip()
    if ";" in value:
        raise CustomDatasetError("Custom dataset SQL must contain exactly one statement.")
    return value


def _normalize_identifier(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('""', '"')
    return value.lower()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
