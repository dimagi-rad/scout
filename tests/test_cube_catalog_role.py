"""The Cube catalog role can read the semantic schema table and nothing else (#421)."""

import importlib
import re
from pathlib import Path

import pytest
from django.db import connection, transaction
from django.db.utils import ProgrammingError

catalog_migration = importlib.import_module("apps.semantic.migrations.0005_cube_catalog_role")
ROLE = catalog_migration.CATALOG_ROLE
CUBE_JS = Path(__file__).resolve().parent.parent / "cube_config" / "cube.js"


def _as_catalog_role(sql):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {ROLE}")
        cursor.execute(sql)
        rows = cursor.fetchall()
        # A released savepoint keeps SET LOCAL for the rest of the test transaction;
        # on failure the savepoint rollback undoes it.
        cursor.execute("RESET ROLE")
    return rows


def test_cube_js_connects_as_the_migrated_role():
    assert re.search(rf"const CATALOG_ROLE = '{ROLE}';", CUBE_JS.read_text())


@pytest.mark.django_db
def test_migration_creates_a_nologin_role_granted_to_the_owner():
    # Read pg_auth_members directly: pg_has_role() is always true for a superuser,
    # which is what CI's database user is.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT r.rolcanlogin, EXISTS ("
            "  SELECT 1 FROM pg_auth_members m JOIN pg_roles u ON u.oid = m.member"
            "  WHERE m.roleid = r.oid AND u.rolname = current_user"
            ") FROM pg_roles r WHERE r.rolname = %s",
            [ROLE],
        )
        assert cursor.fetchall() == [(False, True)]


@pytest.mark.django_db
def test_catalog_role_reads_semantic_cubeschema():
    rows = _as_catalog_role(
        "SELECT filename, content, content_hash, updated_at FROM semantic_cubeschema "
        "WHERE status = 'active' LIMIT 1"
    )
    assert rows == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM workspaces_workspace LIMIT 1",
        "SELECT 1 FROM users_user LIMIT 1",
        "UPDATE semantic_cubeschema SET status = status",
        "DELETE FROM semantic_cubeschema",
    ],
)
def test_catalog_role_cannot_touch_anything_else(sql):
    with pytest.raises(ProgrammingError, match="permission denied"):
        _as_catalog_role(sql)


def _role_ready_sql():
    match = re.search(r"const ROLE_READY_SQL = `(.*?)`;", CUBE_JS.read_text(), re.DOTALL)
    assert match
    return match.group(1).replace("$1", "%s")


@pytest.mark.django_db
@pytest.mark.parametrize(("role", "ready"), [(ROLE, True), ("scout_no_such_role", False)])
def test_cube_js_readiness_check_follows_the_grant(role, ready):
    with connection.cursor() as cursor:
        cursor.execute(_role_ready_sql(), [role, role])
        assert cursor.fetchall() == [(ready,)]


@pytest.mark.django_db
def test_cube_js_readiness_check_is_false_without_this_databases_grant():
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"REVOKE SELECT ON semantic_cubeschema FROM {ROLE}")
        cursor.execute(_role_ready_sql(), [ROLE, ROLE])
        assert cursor.fetchall() == [(False,)]
        transaction.set_rollback(True)
