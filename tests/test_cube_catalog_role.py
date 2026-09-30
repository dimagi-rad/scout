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
        return cursor.fetchall()


def test_cube_js_connects_as_the_migrated_role():
    assert re.search(rf"const CATALOG_ROLE = '{ROLE}';", CUBE_JS.read_text())


@pytest.mark.django_db
def test_migration_creates_a_nologin_role_the_owner_can_assume():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolcanlogin, pg_has_role(current_user, oid, 'MEMBER') "
            "FROM pg_roles WHERE rolname = %s",
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
