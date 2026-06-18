"""
Cube Core connectivity smoke test.

Skipped by default (CI-safe). Activated by setting both:
  - CUBE_URL          e.g. http://localhost:4000
  - CUBEJS_API_SECRET matching the secret Cube is configured with

When active, mints a JWT with {workspace_id, schema_name}, connects to the
Cube SQL API via pg-wire (psycopg), and asserts that SELECT 1 succeeds.

Requires: psycopg (already a project dependency via mcp_server).

Usage:
  CUBE_URL=http://localhost:4000 CUBEJS_API_SECRET=mysecret \
    uv run pytest tests/test_cube_service_smoke.py -v
"""

import os
import time
from urllib.parse import urlparse

import httpx
import jwt
import psycopg
import pytest

# SQL API port is configured via CUBE_SQL_PORT (default 15432).
_CUBE_SQL_PORT = int(os.environ.get("CUBE_SQL_PORT", "15432"))
_CUBE_URL = os.environ.get("CUBE_URL", "")
_CUBEJS_API_SECRET = os.environ.get("CUBEJS_API_SECRET", "")

# Skip the entire module unless both env vars are set.
pytestmark = pytest.mark.skipif(
    not (_CUBE_URL and _CUBEJS_API_SECRET),
    reason=(
        "Cube smoke tests skipped: set CUBE_URL and CUBEJS_API_SECRET "
        "to run against a live Cube instance"
    ),
)


def _mint_jwt(workspace_id: str, schema_name: str) -> str:
    """Mint a short-lived JWT in the shape Scout (Task 8) will produce."""
    payload = {
        "workspace_id": workspace_id,
        "schema_name": schema_name,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,  # 5 minutes
    }
    return jwt.encode(payload, _CUBEJS_API_SECRET, algorithm="HS256")


def _cube_sql_host() -> str:
    """Derive the SQL API hostname from CUBE_URL."""
    parsed = urlparse(_CUBE_URL)
    return parsed.hostname or "localhost"


def test_cube_sql_api_select_one():
    """Connect via pg-wire SQL API, verify JWT auth, run SELECT 1."""
    token = _mint_jwt(
        workspace_id="00000000-0000-0000-0000-000000000001",
        schema_name="t_1",
    )
    host = _cube_sql_host()

    conn = psycopg.connect(
        host=host,
        port=_CUBE_SQL_PORT,
        # Cube SQL API ignores the database name; use a placeholder.
        dbname="scout",
        # Username is unused by our checkSqlAuth (identity is in the JWT).
        user="scout",
        # The JWT is the password — cube.js verifies it in checkSqlAuth.
        password=token,
        sslmode="disable",
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ping")
            row = cur.fetchone()
        assert row is not None, "SELECT 1 returned no rows"
        assert row[0] == 1, f"Expected 1, got {row[0]}"
    finally:
        conn.close()


def test_cube_rest_api_meta():
    """Hit the Cube REST /cubejs-api/v1/meta endpoint to confirm the service is up."""
    meta_url = f"{_CUBE_URL.rstrip('/')}/cubejs-api/v1/meta"
    token = _mint_jwt(
        workspace_id="00000000-0000-0000-0000-000000000001",
        schema_name="t_1",
    )
    resp = httpx.get(
        meta_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
