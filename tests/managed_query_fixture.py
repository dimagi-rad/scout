"""Owned managed-PostgreSQL objects for real MCP executor tests."""

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from mcp_server.context import QueryContext, _parse_db_url


@contextmanager
def managed_query_context():
    url = os.environ.get("MANAGED_DATABASE_URL")
    if not url:
        pytest.skip("MANAGED_DATABASE_URL not set")

    # Roles are cluster-wide, so even separate test databases need distinct names.
    schema_name = f"test_query_exec_{uuid4().hex}"
    ctx = QueryContext(
        tenant_id="test-integration",
        schema_name=schema_name,
        connection_params=_parse_db_url(url, schema_name),
    )
    schema = sql.Identifier(schema_name)
    role = sql.Identifier(ctx.readonly_role)

    with psycopg.connect(**ctx.connection_params, autocommit=True) as conn:
        # A setup failure rolls back our objects; never adopt an existing schema/role.
        with conn.transaction():
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
            conn.execute(
                sql.SQL(
                    "CREATE TABLE {}.items (id SERIAL PRIMARY KEY, name TEXT, value INTEGER)"
                ).format(schema)
            )
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.items (name, value) "
                    "VALUES ('alpha', 1), ('beta', 2), ('gamma', 3)"
                ).format(schema)
            )
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role))
            conn.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema, role)
            )

        try:
            yield ctx
        finally:
            with conn.transaction():
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
                conn.execute(sql.SQL("DROP ROLE {}").format(role))
