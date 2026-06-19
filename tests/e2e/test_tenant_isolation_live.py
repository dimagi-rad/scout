"""Negative tenant-isolation test for cross-opp workspaces (reviewer concern #302).

Proves the least-privilege model behind Cube's intended per-workspace connection: a role
granted USAGE on ONLY workspace A's schemas is *refused* when it queries workspace B's
schema — so isolation is enforced at the database, not merely by Cube's model surface.

Runs against the live managed DB (same gate as the cube e2e suite):
    CUBE_E2E=1 DJANGO_SETTINGS_MODULE=config.settings.development \
        uv run pytest tests/e2e/test_tenant_isolation_live.py -v -m cube_e2e
"""

from __future__ import annotations

import os

import psycopg
import pytest
from django.conf import settings
from psycopg import sql

from apps.workspaces.services.cube_roles import provision_workspace_ro_role

pytestmark = [
    pytest.mark.cube_e2e,
    pytest.mark.skipif(
        not os.getenv("CUBE_E2E"),
        reason="set CUBE_E2E=1 + a running managed DB to run live isolation tests",
    ),
]

_SCHEMA_A = "iso_test_a"
_SCHEMA_B = "iso_test_b"
_WS = "ws_iso_test"


def _exec(cur, statement: str):
    cur.execute(statement)


def test_workspace_role_refused_on_foreign_schema():
    conn = psycopg.connect(settings.MANAGED_DATABASE_URL, autocommit=True)
    role = None
    try:
        with conn.cursor() as cur:
            for s in (_SCHEMA_A, _SCHEMA_B):
                _exec(cur, f"DROP SCHEMA IF EXISTS {s} CASCADE")
                _exec(cur, f"CREATE SCHEMA {s}")
                _exec(cur, f"CREATE TABLE {s}.visits (x int)")
            _exec(cur, f"INSERT INTO {_SCHEMA_A}.visits VALUES (1)")
            _exec(cur, f"INSERT INTO {_SCHEMA_B}.visits VALUES (2)")

            # Scope the workspace role to schema A ONLY.
            role = provision_workspace_ro_role(_WS, [_SCHEMA_A], conn=conn)

            cur.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            try:
                # Allowed: the workspace's own schema.
                cur.execute(f"SELECT x FROM {_SCHEMA_A}.visits")
                assert cur.fetchone()[0] == 1

                # Refused: a foreign workspace's schema — the isolation boundary.
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"SELECT x FROM {_SCHEMA_B}.visits")
            finally:
                cur.execute("RESET ROLE")
    finally:
        with conn.cursor() as cur:
            for s in (_SCHEMA_A, _SCHEMA_B):
                _exec(cur, f"DROP SCHEMA IF EXISTS {s} CASCADE")
            if role:
                cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        conn.close()
