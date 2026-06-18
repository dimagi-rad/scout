"""Live end-to-end tests for the Cube semantic layer.

These tests exercise the FULL stack — real Cube REST + SQL API, real PostgreSQL
managed DB (agent_platform on localhost:5435) — and are therefore:

  * Skipped by default in normal CI (no CUBE_E2E env var set).
  * Run on demand: ``CUBE_E2E=1 uv run pytest tests/e2e -m cube_e2e``

Prerequisites (must all be running before invoking):
  1. PostgreSQL on localhost:5435 (``docker compose up platform-db``)
  2. Cube REST API on localhost:4000/cubejs-api (``docker compose up cube`` or honcho)
  3. Cube SQL API on localhost:15432 (same Cube process, pg-wire port)
  4. Django dev server or at least Django settings loaded (dev settings):
       ``DJANGO_SETTINGS_MODULE=config.settings.development``
  5. Seed data present:
       ``uv run python manage.py seed_demo``
     (the tests call ``seed_demo`` via ``call_command`` to be self-contained,
      but a pre-seeded dev DB avoids the ~18 s Cube model compile on first run)

The tests hit the **dev** ``agent_platform`` database, NOT the pytest test DB.
Django settings are loaded via the ``DJANGO_SETTINGS_MODULE`` env var; the test
DB isolation mechanisms (``@pytest.mark.django_db``) are NOT used here — we
want the same DB rows that Cube is pointed at.

Full run command:
    CUBE_E2E=1 DJANGO_SETTINGS_MODULE=config.settings.development \\
        uv run pytest tests/e2e/test_semantic_layer_live.py -v -m cube_e2e

Skip verification (default CI):
    uv run pytest tests/e2e
"""

from __future__ import annotations

import asyncio
import os

import pytest
from asgiref.sync import async_to_sync
from django.core.management import call_command

# ── Marker / skip gate ────────────────────────────────────────────────────────

pytestmark = [
    pytest.mark.cube_e2e,
    pytest.mark.skipif(
        not os.getenv("CUBE_E2E"),
        reason="set CUBE_E2E=1 + running Cube/DB to run live e2e",
    ),
]

# ── Expected metrics ──────────────────────────────────────────────────────────

# Workspace A (tenant 10001):
#   50 rows, 30 approved → approval_rate=0.60, 35 muac_yes → muac_rate=0.70
EXPECTED_A_COUNT = 50
EXPECTED_A_APPROVAL_RATE = 0.60
EXPECTED_A_MUAC_RATE = 0.70

# Workspace B (tenant 10002):
#   50 rows, 20 approved → approval_rate=0.40, 25 muac_yes → muac_rate=0.50
EXPECTED_B_COUNT = 50
EXPECTED_B_APPROVAL_RATE = 0.40
EXPECTED_B_MUAC_RATE = 0.50

# Cube compile tolerance: allow up to 30 s for a freshly-written model dir.
_CUBE_RETRY_ATTEMPTS = 10
_CUBE_RETRY_SLEEP_S = 3

# ── Helpers ───────────────────────────────────────────────────────────────────

_SEMANTIC_SQL = (
    "SELECT "
    "MEASURE(visits.count), "
    "MEASURE(visits.approval_rate), "
    "MEASURE(visits.muac_confirmation_rate) "
    "FROM visits"
)


async def _query_with_retry(workspace_id: str) -> dict:
    """Run semantic_query with retries so Cube has time to compile a new model."""
    from mcp_server.services.semantic import semantic_query

    last_exc: Exception | None = None
    for attempt in range(_CUBE_RETRY_ATTEMPTS):
        try:
            return await semantic_query(_SEMANTIC_SQL, workspace_id=workspace_id)
        except Exception as exc:
            last_exc = exc
            if attempt < _CUBE_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_CUBE_RETRY_SLEEP_S)
    raise RuntimeError(
        f"semantic_query failed after {_CUBE_RETRY_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


def _run_query(workspace_id: str) -> dict:
    """Sync wrapper around the async semantic_query (for use in sync test functions)."""
    return async_to_sync(_query_with_retry)(workspace_id)


def _extract_metrics(result: dict) -> tuple[int | None, float | None, float | None]:
    """Extract (count, approval_rate, muac_confirmation_rate) from a semantic_query result.

    Column names from Cube use dot-notation, e.g. "visits.count". We match
    by substring to be robust against Cube's exact column aliasing.
    """
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if not rows:
        return None, None, None
    row = rows[0]
    col_map = {col.lower(): row[i] for i, col in enumerate(columns)}

    count_val = None
    approval_val = None
    muac_val = None
    for col, val in col_map.items():
        if "count" in col and "muac" not in col:
            count_val = val
        elif "approval_rate" in col:
            approval_val = val
        elif "muac_confirmation_rate" in col:
            muac_val = val

    return count_val, approval_val, muac_val


def _close_enough(a, b, tol: float = 0.001) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < tol


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def seeded_workspaces():
    """Run seed_demo (idempotent) and return (workspace_a_id, workspace_b_id).

    Calling seed_demo here ensures the test module is self-contained: the dev
    DB and Cube model files are in the expected state regardless of whether a
    human ran seed_demo manually beforehand.

    DB access is globally unblocked by ``allow_database_queries`` in
    ``tests/e2e/conftest.py`` — no ``django_db`` mark or blocker needed here.
    """
    import io

    from apps.workspaces.models import Workspace

    out = io.StringIO()
    call_command("seed_demo", stdout=out, stderr=out)
    ws_a = Workspace.objects.get(name="Demo Workspace")
    ws_b = Workspace.objects.get(name="Demo Workspace B")
    return str(ws_a.id), str(ws_b.id)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestCubePathSmoke:
    """Test A: Cube-path smoke — assert Workspace A returns expected metrics."""

    def test_workspace_a_count_and_rates(self, seeded_workspaces):
        workspace_a_id, _ = seeded_workspaces

        result = _run_query(workspace_a_id)

        assert result.get("row_count", 0) > 0, (
            f"Workspace A returned no rows. Result: {result}"
        )

        count_val, approval_val, muac_val = _extract_metrics(result)

        assert count_val == EXPECTED_A_COUNT, (
            f"Workspace A count mismatch: got {count_val}, expected {EXPECTED_A_COUNT}"
        )
        assert _close_enough(approval_val, EXPECTED_A_APPROVAL_RATE), (
            f"Workspace A approval_rate mismatch: got {approval_val}, "
            f"expected {EXPECTED_A_APPROVAL_RATE}"
        )
        assert _close_enough(muac_val, EXPECTED_A_MUAC_RATE), (
            f"Workspace A muac_confirmation_rate mismatch: got {muac_val}, "
            f"expected {EXPECTED_A_MUAC_RATE}"
        )


class TestTenantIsolation:
    """Test B: Tenant isolation — prove each workspace sees ONLY its own data.

    Security boundary: semantic_query resolves workspace_id → schema_name
    server-side and mints a JWT with that schema in securityContext. Cube's
    checkSqlAuth wires the JWT's schema_name into COMPILE_CONTEXT so that
    ``{COMPILE_CONTEXT.security_context.schema_name}.stg_visits`` resolves to
    the correct per-tenant table. A query through workspace A's token cannot
    reach workspace B's rows and vice versa.

    This test asserts that:
      * Workspace A → metrics 0.60 / 0.70  (not B's 0.40 / 0.50)
      * Workspace B → metrics 0.40 / 0.50  (not A's 0.60 / 0.70)
    """

    def test_workspace_a_isolation(self, seeded_workspaces):
        """Workspace A's query returns A's distinct metrics (0.60 / 0.70)."""
        workspace_a_id, _ = seeded_workspaces

        result_a = _run_query(workspace_a_id)
        count_a, approval_a, muac_a = _extract_metrics(result_a)

        assert count_a == EXPECTED_A_COUNT, (
            f"Workspace A count: got {count_a}, expected {EXPECTED_A_COUNT}"
        )
        assert _close_enough(approval_a, EXPECTED_A_APPROVAL_RATE), (
            f"Workspace A approval_rate: got {approval_a}, "
            f"expected {EXPECTED_A_APPROVAL_RATE} — possible cross-tenant leak"
        )
        assert _close_enough(muac_a, EXPECTED_A_MUAC_RATE), (
            f"Workspace A muac_rate: got {muac_a}, expected {EXPECTED_A_MUAC_RATE} "
            "— possible cross-tenant leak"
        )

    def test_workspace_b_isolation(self, seeded_workspaces):
        """Workspace B's query returns B's distinct metrics (0.40 / 0.50)."""
        _, workspace_b_id = seeded_workspaces

        result_b = _run_query(workspace_b_id)
        count_b, approval_b, muac_b = _extract_metrics(result_b)

        assert count_b == EXPECTED_B_COUNT, (
            f"Workspace B count: got {count_b}, expected {EXPECTED_B_COUNT}"
        )
        assert _close_enough(approval_b, EXPECTED_B_APPROVAL_RATE), (
            f"Workspace B approval_rate: got {approval_b}, "
            f"expected {EXPECTED_B_APPROVAL_RATE} — possible cross-tenant leak"
        )
        assert _close_enough(muac_b, EXPECTED_B_MUAC_RATE), (
            f"Workspace B muac_rate: got {muac_b}, expected {EXPECTED_B_MUAC_RATE} "
            "— possible cross-tenant leak"
        )

    def test_workspaces_have_different_metrics(self, seeded_workspaces):
        """The two workspaces return distinctly different metrics (sanity check).

        If both workspaces return identical approval_rate values it indicates
        either the seed data is broken (not writing to separate schemas) or
        the JWT isolation is not working (both schemas have the same data).
        """
        workspace_a_id, workspace_b_id = seeded_workspaces

        result_a = _run_query(workspace_a_id)
        result_b = _run_query(workspace_b_id)

        _, approval_a, muac_a = _extract_metrics(result_a)
        _, approval_b, muac_b = _extract_metrics(result_b)

        assert not _close_enough(approval_a, approval_b, tol=0.01), (
            f"Workspaces A and B returned the same approval_rate ({approval_a}). "
            "Tenant isolation may be broken."
        )
        assert not _close_enough(muac_a, muac_b, tol=0.01), (
            f"Workspaces A and B returned the same muac_confirmation_rate ({muac_a}). "
            "Tenant isolation may be broken."
        )
