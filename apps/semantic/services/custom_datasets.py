"""Validation and column inference for workspace-authored custom datasets."""

from __future__ import annotations

from typing import Any

import psycopg
from asgiref.sync import async_to_sync
from psycopg import sql as psql
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope

from mcp_server.context import load_workspace_context
from mcp_server.services.sql_validator import SQLValidationError, SQLValidator


class CustomDatasetError(ValueError):
    """Raised when a custom dataset cannot be compiled safely."""


def compile_custom_dataset_sql(
    definition_sql: str,
    *,
    allowed_tables: dict[str, str],
) -> str:
    """Return a workspace-scoped SELECT statement for a custom dataset.

    ``allowed_tables`` maps lower-case semantic dataset names and physical table
    names to the real physical table/view name in the workspace query schema.
    Table references stay schema-unqualified on purpose: both execution paths
    (Cube's driver and infer_custom_dataset_columns) set search_path to the
    workspace's current schema, so a tenant-schema swap never stales this SQL.
    """
    statement, tables = _custom_dataset_tables(definition_sql)
    for table in tables:
        table_key = table.name.lower()
        table_name = allowed_tables.get(table_key)
        if not table_name:
            raise CustomDatasetError(
                f"Custom dataset SQL references unknown workspace table '{table_key}'."
            )
        if table.name != table_name and not table.alias:
            table.set("alias", exp.TableAlias(this=table.this.copy()))
        table.set("this", exp.to_identifier(table_name, quoted=True))

    return statement.sql(dialect="postgres")


def custom_dataset_dependencies(definition_sql: str) -> set[str]:
    """Physical references from the same validated scopes used by compilation."""
    _statement, tables = _custom_dataset_tables(definition_sql)
    return {table.name.lower() for table in tables}


def _custom_dataset_tables(definition_sql: str):
    if not (definition_sql or "").strip():
        raise CustomDatasetError("Custom dataset SQL is required.")
    try:
        # Share the read-only/function safety boundary with raw queries, but do
        # not inject their display row limit into a reusable dataset (#406).
        statement = SQLValidator().validate(definition_sql)
    except SQLValidationError as exc:
        raise CustomDatasetError(f"Custom dataset SQL is invalid: {exc}") from exc

    normalize_identifiers(statement, dialect="postgres")
    for table in statement.find_all(exp.Table):
        if table.db or table.catalog:
            raise CustomDatasetError("Custom dataset SQL cannot use explicit schema references.")

    try:
        tables = [
            source
            for scope in traverse_scope(statement)
            for _node, source in scope.selected_sources.values()
            if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier)
        ]
    except OptimizeError as exc:
        raise CustomDatasetError(f"Custom dataset SQL is invalid: {exc}") from exc

    return statement, tables


def infer_custom_dataset_columns(workspace, compiled_sql: str) -> list[dict[str, Any]]:
    """Probe a compiled custom dataset query and return catalog-style columns."""
    ctx = async_to_sync(load_workspace_context)(str(workspace.id))
    try:
        with (
            psycopg.connect(**ctx.connection_params, autocommit=True) as conn,
            conn.cursor() as cursor,
        ):
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
