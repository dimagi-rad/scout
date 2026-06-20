"""DB-layer isolation via Cube's driverFactory connection mechanism (reviewer #302, §7).

`test_tenant_isolation_live` proves the role is refused on a foreign schema via ``SET ROLE``.
This proves the SAME guarantee holds through the EXACT mechanism Cube's ``driverFactory`` now
uses: a connection opened with the libpq ``options=-c role=<schema>_ro`` parameter (applied at
connect for every pooled connection) runs as the workspace's read-only role, so it can read the
workspace's own schema but is refused on a foreign one. Together with cube.js pinning each
``ws_<hash>`` pool to ``<schema_name>_ro``, this is the database-enforced tenant boundary —
not just Cube's model surface.

Runs against the live managed DB (same gate as the cube e2e suite):
    CUBE_E2E=1 DJANGO_SETTINGS_MODULE=config.settings.development \
        uv run pytest tests/e2e/test_cube_role_isolation_live.py -v -m cube_e2e
"""

from __future__ import annotations

import os

import psycopg
import pytest
from django.conf import settings

from apps.workspaces.services.cube_roles import provision_workspace_ro_role

pytestmark = [
    pytest.mark.cube_e2e,
    pytest.mark.skipif(
        not os.getenv("CUBE_E2E"),
        reason="set CUBE_E2E=1 + a running managed DB to run live isolation tests",
    ),
]

_SCHEMA_OWN = "drv_iso_own"
_SCHEMA_FOREIGN = "drv_iso_foreign"
_WS = "ws_drv_iso_test"


def test_role_pinned_connection_reads_own_refuses_foreign():
    """A connection opened the way driverFactory opens it (`-c role=<ro>`) is DB-isolated."""
    admin = psycopg.connect(settings.MANAGED_DATABASE_URL, autocommit=True)
    role = None
    try:
        with admin.cursor() as cur:
            for s in (_SCHEMA_OWN, _SCHEMA_FOREIGN):
                cur.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
                cur.execute(f"CREATE SCHEMA {s}")
                cur.execute(f"CREATE TABLE {s}.visits (x int)")
            cur.execute(f"INSERT INTO {_SCHEMA_OWN}.visits VALUES (1)")
            cur.execute(f"INSERT INTO {_SCHEMA_FOREIGN}.visits VALUES (2)")
            # Role scoped to the workspace's OWN schema only.
            role = provision_workspace_ro_role(_WS, [_SCHEMA_OWN], conn=admin)

        # Open a SECOND connection exactly as cube.js driverFactory does: pin the
        # session role at connect via the libpq options parameter. No SET ROLE call.
        pinned = psycopg.connect(
            settings.MANAGED_DATABASE_URL,
            options=f"-c role={role}",
            autocommit=True,
        )
        try:
            with pinned.cursor() as cur:
                cur.execute("SELECT current_user, current_setting('role')")
                _user, current_role = cur.fetchone()
                assert current_role == role  # the connection really runs as the RO role

                # Allowed: the workspace's own schema.
                cur.execute(f"SELECT x FROM {_SCHEMA_OWN}.visits")
                assert cur.fetchone()[0] == 1

                # Refused: a foreign schema — the database enforces the boundary.
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"SELECT x FROM {_SCHEMA_FOREIGN}.visits")
        finally:
            pinned.close()
    finally:
        with admin.cursor() as cur:
            for s in (_SCHEMA_OWN, _SCHEMA_FOREIGN):
                cur.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
            if role:
                cur.execute(f'DROP OWNED BY "{role}"')
                cur.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()
