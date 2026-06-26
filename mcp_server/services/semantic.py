"""
Semantic layer service for the MCP server.

Provides Cube-backed semantic SQL query execution and catalog discovery,
scoped per-workspace via short-lived JWTs passed as the Cube SQL API password.

Multi-tenant auth flow:
  1. Resolve the workspace's schema_name via load_workspace_context (single
     source of truth — same as the raw query path).
  2. Mint a short-lived JWT (HS256, signed with CUBEJS_API_SECRET) carrying
     {workspace_id, schema_name, exp}. Cube's checkSqlAuth verifies this JWT
     and selects the per-workspace model + schema via COMPILE_CONTEXT.
  3. Connect to Cube's pg-wire SQL API with the JWT as the password.
     The agent never sees the schema name or JWT — both are injected server-side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import psycopg
from django.conf import settings

from mcp_server.context import load_workspace_context


def mint_cube_jwt(workspace_id: str, schema_name: str, *, ttl_seconds: int = 300) -> str:
    """Mint a short-lived JWT for Cube SQL/REST API authentication.

    The JWT is signed with CUBEJS_API_SECRET (HS256). Cube's checkSqlAuth
    verifies it and populates COMPILE_CONTEXT with {workspace_id, schema_name}
    so Cube selects the correct per-workspace model and schema.

    Claim names MUST be exactly ``workspace_id`` and ``schema_name`` to match
    the cube.js checkSqlAuth implementation.

    Args:
        workspace_id: Workspace UUID string.
        schema_name: PostgreSQL schema name for this workspace (e.g. ``t_abc``).
        ttl_seconds: Token lifetime in seconds (default 300 = 5 minutes).

    Returns:
        Encoded JWT string.
    """
    now = datetime.now(tz=UTC)
    payload = {
        "workspace_id": workspace_id,
        "schema_name": schema_name,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, settings.CUBEJS_API_SECRET, algorithm="HS256")


async def semantic_query(sql: str, workspace_id: str = "") -> dict[str, Any]:
    """Execute a Semantic SQL query against the Cube SQL API for the workspace.

    Resolves the workspace's schema via load_workspace_context, mints a JWT,
    connects to Cube's pg-wire SQL API (password = JWT), and returns the same
    envelope shape as the raw ``query`` tool (columns, rows, row_count,
    sql_executed).

    Args:
        sql: A Semantic SQL query (may use MEASURE(...), DIMENSION(...) etc.).
        workspace_id: Workspace UUID (injected server-side by the agent graph).

    Returns:
        Dict with keys: columns, rows, row_count, sql_executed.

    Raises:
        ValueError: If workspace_id is empty or workspace not found.
        psycopg.Error: On Cube SQL API connection/query failure.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required")

    ctx = await load_workspace_context(workspace_id)
    token = mint_cube_jwt(workspace_id, ctx.schema_name)

    async with await psycopg.AsyncConnection.connect(
        host=settings.CUBE_SQL_HOST,
        port=settings.CUBE_SQL_PORT,
        user="scout",
        password=token,
        dbname="cube",
        autocommit=True,
        sslmode="disable",
    ) as conn, conn.cursor() as cursor:
        await cursor.execute(sql)

        columns: list[str] = []
        rows: list[list[Any]] = []

        if cursor.description:
            columns = [desc[0] for desc in cursor.description]
            rows = [list(row) for row in await cursor.fetchall()]

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "sql_executed": sql,
    }


async def semantic_catalog(workspace_id: str = "") -> dict[str, Any]:
    """Fetch the Cube semantic catalog (cubes/views, measures, dimensions).

    Resolves the workspace schema, mints a JWT, and calls the Cube REST API
    ``GET /cubejs-api/v1/meta`` with ``Authorization: Bearer <JWT>``. Returns a
    compact dict of available cubes and their measures & dimensions so the agent
    knows what semantic queries it can construct.

    Args:
        workspace_id: Workspace UUID (injected server-side by the agent graph).

    Returns:
        Dict with key ``cubes``: list of cube dicts each containing ``name``,
        ``measures``, and ``dimensions`` lists.

    Raises:
        ValueError: If workspace_id is empty or workspace not found.
        httpx.HTTPError: On Cube REST API failure.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required")

    ctx = await load_workspace_context(workspace_id)
    token = mint_cube_jwt(workspace_id, ctx.schema_name)

    meta_url = f"{settings.CUBE_REST_URL.rstrip('/')}/v1/meta"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            meta_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

    # Shape into a compact representation the agent can read.
    cubes_raw = data.get("cubes", [])
    cubes = []
    for cube in cubes_raw:
        measures = [
            {"name": m["name"], "title": m.get("title", ""), "type": m.get("type", "")}
            for m in cube.get("measures", [])
        ]
        dimensions = [
            {"name": d["name"], "title": d.get("title", ""), "type": d.get("type", "")}
            for d in cube.get("dimensions", [])
        ]
        cubes.append(
            {
                "name": cube["name"],
                "title": cube.get("title", cube["name"]),
                "measures": measures,
                "dimensions": dimensions,
            }
        )

    return {"cubes": cubes}
