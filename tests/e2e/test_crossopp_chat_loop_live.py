"""Live e2e test: chat-driven measure define → commit → semantic_query returns it.

Exercises the full loop against the live KMC Cross-Opp workspace and Cube:
  1. Call define_crossopp_measure (the agent tool) with a fresh measure name.
  2. Assert the tool either commits or flags needs_approval.
  3. If committed, run semantic_query and assert at least 1 row is returned.

Skipped by default in CI (CUBE_E2E not set) or when the KMC Cross-Opp workspace
does not exist in the dev DB.

Run:
    CUBE_E2E=1 DJANGO_SETTINGS_MODULE=config.settings.development \\
        uv run pytest tests/e2e/test_crossopp_chat_loop_live.py -v -m cube_e2e
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

pytestmark = [
    pytest.mark.cube_e2e,
    pytest.mark.skipif(
        not os.getenv("CUBE_E2E"),
        reason="set CUBE_E2E=1 + running Cube/DB to run live e2e",
    ),
]

# Retry parameters: Cube may need a moment to compile a newly-written model.
_CUBE_RETRY_ATTEMPTS = 10
_CUBE_RETRY_SLEEP_S = 3

# Measure used by this test — must NOT collide with the four starter measures
# (birth_weight, mortality, kmc_hours, danger_sign_referral_rate).
_TEST_MEASURE_NAME = "successful_feeds"
_TEST_MEASURE_DESC = "successful feeds in the last 24 hours"
_TEST_MEASURE_KIND = "numeric"


async def _query_with_retry(sql: str, workspace_id: str) -> dict:
    """Run semantic_query with retries so Cube has time to compile a new model."""
    from mcp_server.services.semantic import semantic_query

    last_exc: Exception | None = None
    for attempt in range(_CUBE_RETRY_ATTEMPTS):
        try:
            return await semantic_query(sql, workspace_id=workspace_id)
        except Exception as exc:
            last_exc = exc
            if attempt < _CUBE_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_CUBE_RETRY_SLEEP_S)
    raise RuntimeError(
        f"semantic_query failed after {_CUBE_RETRY_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


@pytest.mark.asyncio
async def test_define_then_query_new_measure():
    """define_crossopp_measure on a fresh measure -> committed -> semantic_query returns rows."""
    from apps.users.models import User
    from apps.workspaces.models import Workspace

    # --- resolve live workspace (skip if not present) ---
    try:
        workspace = await Workspace.objects.aget(name="KMC Cross-Opp")
    except Workspace.DoesNotExist:
        pytest.skip(
            "KMC Cross-Opp workspace not found in dev DB — run build_crossopp_workspace first"
        )

    try:
        user = await User.objects.aget(email="admin@example.com")
    except User.DoesNotExist:
        pytest.skip("admin@example.com user not found in dev DB — run seed_connect_labs first")

    from apps.agents.tools.crossopp_measure_tool import create_crossopp_measure_tools

    [define, _propose] = create_crossopp_measure_tools(workspace, user, "e2e-thread")

    # --- invoke the tool ---
    try:
        out = await define.ainvoke(
            {
                "name": _TEST_MEASURE_NAME,
                "description": _TEST_MEASURE_DESC,
                "kind": _TEST_MEASURE_KIND,
            }
        )
    except (ImportError, AttributeError, TypeError, NameError, KeyError):
        raise  # code bug — must fail, not skip
    except httpx.HTTPError as exc:
        pytest.skip(f"live Cube/LLM transport error: {exc}")
    except Exception as exc:
        # external LLM/provider error — skip, but only as a last resort
        pytest.skip(f"live resolve unavailable: {exc}")

    status = out.get("status")

    if status == "exists":
        # Measure was already defined in a prior run — that's fine; still query it.
        pass
    elif status == "needs_approval":
        pytest.skip("resolver had doubt on this measure; approval path is covered by the API test")
    elif status != "committed":
        pytest.fail(f"Unexpected tool status: {status!r}. Full output: {out}")

    # --- semantic_query must return >=1 row ---
    semantic_sql = (
        f"SELECT opportunity_id, MEASURE(kmc_cross_opp.{_TEST_MEASURE_NAME}) "
        f"FROM kmc_cross_opp GROUP BY 1"
    )

    try:
        result = await _query_with_retry(semantic_sql, workspace_id=str(workspace.id))
    except Exception as exc:
        pytest.fail(
            f"semantic_query failed after retries: {exc}\n"
            "Check that Cube is running and the model compiled successfully."
        )

    assert result.get("rows"), (
        f"semantic_query returned no rows for measure '{_TEST_MEASURE_NAME}'. Result: {result}"
    )
