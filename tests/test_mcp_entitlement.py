"""MCP tool entitlement scoping (arch #253, finding 01#6).

Tenant-scoped MCP tools previously trusted whatever id the caller supplied:

* ``run_materialization``'s membership guard PASSED for any user when
  ``user_id=''`` (the empty-user_id filter was skipped, returning every
  membership in the workspace).
* ``cancel_materialization`` / ``get_materialization_status`` accepted any
  LLM-supplied ``run_id`` and resolved a ``MaterializationRun`` from ANY
  workspace — a run in workspace B could be cancelled/inspected from a chat
  scoped to workspace A.

These tests pin the fix: an empty user_id is denied, and cancel/status are
scoped to the calling workspace.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.contrib.auth import get_user_model

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceTenant,
)
from mcp_server.server import (
    cancel_materialization,
    get_materialization_status,
    run_materialization,
)

User = get_user_model()


async def _make_workspace_with_run(*, email, ext_id, schema_name, run_state):
    """Create a workspace + tenant + active schema + one materialization run."""
    user = await User.objects.acreate_user(email=email, password="x")
    ws = await Workspace.objects.acreate(name=f"W-{ext_id}", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id=ext_id, provider="commcare", canonical_name=f"T-{ext_id}"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(tenant=tenant, user=user)
    ts = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=schema_name, state=SchemaState.ACTIVE
    )
    run = await MaterializationRun.objects.acreate(
        tenant_schema=ts,
        pipeline="commcare_sync",
        state=run_state,
        started_at=datetime.now(UTC),
    )
    return user, ws, run


# --- run_materialization user_id='' bypass ---


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_run_materialization_denies_empty_user_id():
    """An empty user_id must be denied — it previously passed the membership
    guard for any user (the empty filter returned every membership)."""
    user = await User.objects.acreate_user(email="empty@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-empty", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="te", provider="commcare", canonical_name="Empty Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await TenantMembership.objects.acreate(tenant=tenant, user=user)
    from apps.chat.models import Thread

    thread = await Thread.objects.acreate(workspace=ws, user=user)

    result = await run_materialization(
        workspace_id=str(ws.id),
        user_id="",  # the bypass: empty user_id
        thread_id=str(thread.id),
        tool_call_id="tc-empty",
    )

    assert result["success"] is False
    assert result["error"]["code"] in {"VALIDATION_ERROR", "NOT_FOUND"}


# --- cancel_materialization scoping ---


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_materialization_rejects_run_from_other_workspace():
    _, _ws_a, _ = await _make_workspace_with_run(
        email="a@b.c", ext_id="ta", schema_name="t_a", run_state=MaterializationRun.RunState.STARTED
    )
    _, ws_b, run_b = await _make_workspace_with_run(
        email="b@b.c", ext_id="tb", schema_name="t_b", run_state=MaterializationRun.RunState.STARTED
    )

    # Caller is scoped to workspace A but supplies workspace B's run_id.
    result = await cancel_materialization(run_id=str(run_b.id), workspace_id=str(_ws_a.id))

    assert result["success"] is False
    assert result["error"]["code"] == "NOT_FOUND"
    # The run in B must NOT have been cancelled.
    await run_b.arefresh_from_db()
    assert run_b.state == MaterializationRun.RunState.STARTED
    assert ws_b  # silence unused


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_cancel_materialization_allows_run_in_own_workspace():
    _, ws, run = await _make_workspace_with_run(
        email="own@b.c",
        ext_id="to",
        schema_name="t_o",
        run_state=MaterializationRun.RunState.STARTED,
    )

    result = await cancel_materialization(run_id=str(run.id), workspace_id=str(ws.id))

    assert result["success"] is True
    await run.arefresh_from_db()
    assert run.state == MaterializationRun.RunState.CANCELLED


# --- get_materialization_status scoping ---


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_status_rejects_run_from_other_workspace():
    _, ws_a, _ = await _make_workspace_with_run(
        email="sa@b.c",
        ext_id="tsa",
        schema_name="t_sa",
        run_state=MaterializationRun.RunState.COMPLETED,
    )
    _, _ws_b, run_b = await _make_workspace_with_run(
        email="sb@b.c",
        ext_id="tsb",
        schema_name="t_sb",
        run_state=MaterializationRun.RunState.COMPLETED,
    )

    result = await get_materialization_status(run_id=str(run_b.id), workspace_id=str(ws_a.id))

    assert result["success"] is False
    assert result["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_status_allows_run_in_own_workspace():
    _, ws, run = await _make_workspace_with_run(
        email="so@b.c",
        ext_id="tso",
        schema_name="t_so",
        run_state=MaterializationRun.RunState.COMPLETED,
    )

    result = await get_materialization_status(run_id=str(run.id), workspace_id=str(ws.id))

    assert result["success"] is True
    assert result["data"]["run_id"] == str(run.id)
