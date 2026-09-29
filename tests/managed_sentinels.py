"""Sentinel tables and cleanup for tests that load into real managed PostgreSQL."""

import contextlib

import psycopg
import psycopg.sql

from apps.common.identifiers import readonly_role_name
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection


def write_sentinel(schema_name: str, value: str) -> None:
    """Replace ``schema_name.raw_cases`` with one row holding ``value``."""
    ident = psycopg.sql.Identifier(schema_name)
    with get_managed_db_connection() as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident))
        conn.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {}.raw_cases").format(ident))
        conn.execute(psycopg.sql.SQL("CREATE TABLE {}.raw_cases (value text)").format(ident))
        conn.execute(
            psycopg.sql.SQL("INSERT INTO {}.raw_cases VALUES ({})").format(
                ident, psycopg.sql.Literal(value)
            )
        )


def read_as_readonly(view_schema: str, relation: str) -> list[str]:
    """Read ``relation`` as the view schema's read-only role, as queries would."""
    with get_managed_db_connection() as conn:
        conn.execute(
            psycopg.sql.SQL("SET ROLE {}").format(
                psycopg.sql.Identifier(readonly_role_name(view_schema))
            )
        )
        rows = conn.execute(
            psycopg.sql.SQL("SELECT value FROM {}.{}").format(
                psycopg.sql.Identifier(view_schema), psycopg.sql.Identifier(relation)
            )
        ).fetchall()
    return [row[0] for row in rows]


def drop_owned(names: list[str]) -> None:
    """Drop the schemas and their roles; best effort, since a failed test may not have made them."""
    manager = SchemaManager()
    with get_managed_db_connection() as conn:
        cursor = conn.cursor()
        for name in names:
            with contextlib.suppress(Exception):
                cursor.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(name)
                    )
                )
        for name in names:
            with contextlib.suppress(Exception):
                manager._drop_readonly_role(cursor, name)
                manager._drop_dbt_role(cursor, name)
