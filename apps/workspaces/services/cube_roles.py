"""Least-privilege Postgres roles for cross-opp workspaces (tenant isolation).

Cube currently connects to Postgres as the ``platform`` superuser, so isolation rests
entirely on Cube's model surface (reviewer concern #302). This provisions a per-workspace
**read-only** role granted USAGE + SELECT on ONLY that workspace's constituent tenant
schemas — nothing else. Cube should connect as this role per request (via a ``driverFactory``
keyed on the JWT's ``schema_name``), so a query issued for one workspace can never reach
another workspace's data even if a model were misconfigured.

``provision_workspace_ro_role`` builds/refreshes the role; the negative-isolation test
(``tests/e2e/test_tenant_isolation_live.py``) proves a role scoped to workspace A's schemas
is refused on workspace B's schema.
"""

from __future__ import annotations

import psycopg
from django.conf import settings
from psycopg import sql

from apps.common.identifiers import readonly_role_name


def provision_workspace_ro_role(
    view_schema_name: str,
    tenant_schema_names: list[str],
    *,
    conn: psycopg.Connection | None = None,
) -> str:
    """Create/refresh the read-only role for a workspace and grant it USAGE + SELECT on
    ONLY ``tenant_schema_names``. Idempotent. Returns the role name.

    The role is ``NOLOGIN``: Cube's base connection role is granted membership and assumes it
    via ``SET ROLE`` per request (or a ``driverFactory`` connects with it) — there is no
    per-workspace password to manage.
    """
    role = readonly_role_name(view_schema_name)
    own_conn = conn is None
    if own_conn:
        conn = psycopg.connect(settings.MANAGED_DATABASE_URL, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            for schema in tenant_schema_names:
                cur.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    )
                )
                cur.execute(
                    sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    )
                )
                # New tables in these schemas (e.g. a re-materialized stg_visits) inherit SELECT.
                cur.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
                    ).format(sql.Identifier(schema), sql.Identifier(role))
                )
        return role
    finally:
        if own_conn:
            conn.close()
