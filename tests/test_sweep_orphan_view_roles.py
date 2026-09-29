"""sweep_orphan_view_roles against the real managed database (#420).

The managed database is shared across checkouts, so every --apply run is scoped
with --role to roles this test created.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from io import StringIO

import psycopg
import psycopg.sql
import pytest
from django.core.management import call_command

from apps.common.identifiers import dbt_role_name, readonly_role_name
from apps.workspaces.models import SchemaState, WorkspaceViewSchema
from apps.workspaces.services.schema_manager import SchemaManager, get_managed_db_connection

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not os.environ.get("MANAGED_DATABASE_URL"),
        reason="MANAGED_DATABASE_URL not set",
    ),
]

Q = psycopg.sql.SQL
ID = psycopg.sql.Identifier


@pytest.fixture
def managed():
    conn = get_managed_db_connection()
    created = {"schemas": [], "roles": []}
    yield conn, created
    try:
        with conn.cursor() as cur:
            for schema in created["schemas"]:
                with contextlib.suppress(psycopg.Error):
                    cur.execute(Q("DROP SCHEMA IF EXISTS {} CASCADE").format(ID(schema)))
            for role in created["roles"]:
                with contextlib.suppress(psycopg.Error):
                    cur.execute(Q("DROP ROLE IF EXISTS {}").format(ID(role)))
    finally:
        conn.close()


def _role_exists(conn, role):
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()


def _sweep(*args):
    out = StringIO()
    call_command("sweep_orphan_view_roles", *args, stdout=out)
    return out.getvalue()


def _orphan_view_roles(conn, created):
    """Reproduce #420: teardown dropped the schema, but a legacy grant and default
    ACL on a tenant schema made its best-effort DROP ROLE fail."""
    schema = f"ws_{uuid.uuid4().hex[:16]}"
    tenant_schema = f"sweep_t_{uuid.uuid4().hex[:8]}"
    ro, dbt = readonly_role_name(schema), dbt_role_name(schema)
    created["schemas"] += [schema, tenant_schema]
    created["roles"] += [ro, dbt]
    with conn.cursor() as cur:
        cur.execute(Q("CREATE SCHEMA {}").format(ID(schema)))
        SchemaManager()._create_readonly_role(cur, schema)
        cur.execute(Q("CREATE SCHEMA {}").format(ID(tenant_schema)))
        cur.execute(Q("CREATE TABLE {}.t (id int)").format(ID(tenant_schema)))
        cur.execute(Q("GRANT SELECT ON {}.t TO {}").format(ID(tenant_schema), ID(ro)))
        cur.execute(
            Q("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
                ID(tenant_schema), ID(ro)
            )
        )
        cur.execute(Q("DROP SCHEMA {} CASCADE").format(ID(schema)))
    return ro, dbt


def test_orphan_is_reported_by_default_and_dropped_with_apply(managed):
    conn, created = managed
    ro, dbt = _orphan_view_roles(conn, created)

    report = _sweep("--role", ro, "--role", dbt)
    assert f"{ro}: would drop" in report
    assert f"{dbt}: would drop" in report
    assert _role_exists(conn, ro) and _role_exists(conn, dbt)

    applied = _sweep("--apply", "--role", ro, "--role", dbt)
    assert f"{ro}: dropped" in applied and f"{dbt}: dropped" in applied
    assert not _role_exists(conn, ro)
    assert not _role_exists(conn, dbt)


def test_live_view_schema_role_is_kept(managed, workspace):
    conn, created = managed
    schema = SchemaManager()._view_schema_name(workspace.id)
    WorkspaceViewSchema.objects.create(
        workspace=workspace, schema_name=schema, state=SchemaState.ACTIVE
    )
    ro = readonly_role_name(schema)
    created["roles"].append(ro)
    conn.execute(Q("CREATE ROLE {} NOLOGIN").format(ID(ro)))

    assert f"{ro}: kept" in _sweep("--apply", "--role", ro)
    assert _role_exists(conn, ro)


def test_roles_outside_the_generated_pattern_are_untouched(managed):
    conn, created = managed
    hex_id = uuid.uuid4().hex[:16]
    lookalike = f"ws_{hex_id}_rw"
    login_role = f"ws_{uuid.uuid4().hex[:16]}_ro"
    created["roles"] += [lookalike, login_role]
    conn.execute(Q("CREATE ROLE {} NOLOGIN").format(ID(lookalike)))
    conn.execute(Q("CREATE ROLE {} LOGIN").format(ID(login_role)))

    report = _sweep("--apply", "--role", lookalike, "--role", login_role)

    assert f"{lookalike}: ignored" in report
    assert f"{login_role}: refused" in report
    assert _role_exists(conn, lookalike)
    assert _role_exists(conn, login_role)
