"""MCP data tools re-check the acting user's workspace access on every call.

The raw-SQL and metadata tools used to trust the workspace id the chat turn
injected after its own check, so a turn or resumed job that outlived the user's
access kept reading. They now go back through the central authorizer.
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.chat.models import Thread, ThreadJob
from apps.users.models import TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
)
from mcp_server import server
from mcp_server.context import QueryContext

User = get_user_model()

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


def _context():
    return QueryContext(tenant_id="t", schema_name="t_schema", connection_params={})


async def _query(workspace, user_id):
    with (
        patch.object(server, "load_workspace_context", AsyncMock(return_value=_context())) as load,
        patch.object(
            server,
            "execute_query",
            AsyncMock(return_value={"columns": [], "rows": [], "row_count": 0}),
        ) as run,
    ):
        result = await server.query("SELECT 1", workspace_id=str(workspace.id), user_id=user_id)
    return result, load, run


async def test_member_with_access_reads(workspace, user):
    result, load, run = await _query(workspace, str(user.id))

    assert result.get("success", True) is not False
    load.assert_awaited_once()
    run.assert_awaited_once()


async def test_non_member_is_refused_before_any_read(workspace):
    stranger = await User.objects.acreate_user(email="stranger@example.com", password="x")

    result, load, run = await _query(workspace, str(stranger.id))

    assert result["error"]["code"] == "WORKSPACE_ACCESS_DENIED"
    load.assert_not_awaited()
    run.assert_not_awaited()


async def test_member_who_lost_the_source_is_told_which_one(workspace, user, tenant):
    await TenantMembership.objects.filter(user=user, tenant=tenant).aupdate(
        archived_at=timezone.now()
    )

    result, load, _run = await _query(workspace, str(user.id))

    assert result["error"]["code"] == "WORKSPACE_ACCESS_DENIED"
    assert tenant.canonical_name in result["error"]["message"]
    load.assert_not_awaited()


@pytest.mark.parametrize("tool", ["list_tables", "get_metadata", "get_schema_status"])
async def test_every_data_tool_rechecks(workspace, tool):
    stranger = await User.objects.acreate_user(email="stranger@example.com", password="x")

    with patch.object(server, "load_workspace_context", AsyncMock(return_value=_context())) as load:
        result = await getattr(server, tool)(
            workspace_id=str(workspace.id), user_id=str(stranger.id)
        )

    assert result["error"]["code"] == "WORKSPACE_ACCESS_DENIED"
    load.assert_not_awaited()


async def test_describe_table_rechecks(workspace):
    stranger = await User.objects.acreate_user(email="stranger@example.com", password="x")

    with patch.object(server, "load_workspace_context", AsyncMock(return_value=_context())) as load:
        result = await server.describe_table(
            "cases", workspace_id=str(workspace.id), user_id=str(stranger.id)
        )

    assert result["error"]["code"] == "WORKSPACE_ACCESS_DENIED"
    load.assert_not_awaited()


async def test_calls_without_an_acting_user_are_unchanged(workspace):
    result, load, _run = await _query(workspace, "")

    assert result.get("success", True) is not False
    load.assert_awaited_once()


async def test_get_lineage_rechecks(workspace):
    stranger = await User.objects.acreate_user(email="stranger@example.com", password="x")

    with patch.object(server, "aget_lineage_chain", AsyncMock(return_value=[])) as lineage:
        result = await server.get_lineage(
            "cases", workspace_id=str(workspace.id), user_id=str(stranger.id)
        )

    assert result["error"]["code"] == "WORKSPACE_ACCESS_DENIED"
    lineage.assert_not_awaited()


async def _status_run(workspace, tenant):
    tenant_schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="t_secret_schema", state=SchemaState.ACTIVE
    )
    return await MaterializationRun.objects.acreate(
        tenant_schema=tenant_schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.LOADING,
        procrastinate_job_id=4242,
    )


async def test_materialization_status_reads_for_a_member_with_access(workspace, user, tenant):
    run = await _status_run(workspace, tenant)

    result = await server.get_materialization_status(
        str(run.id), workspace_id=str(workspace.id), user_id=str(user.id)
    )

    assert result["success"] is True
    assert result["schema"] == "t_secret_schema"


async def test_materialization_status_denies_a_member_who_lost_the_source(workspace, user, tenant):
    run = await _status_run(workspace, tenant)
    await TenantMembership.objects.filter(user=user, tenant=tenant).aupdate(
        archived_at=timezone.now()
    )

    result = await server.get_materialization_status(
        str(run.id), workspace_id=str(workspace.id), user_id=str(user.id)
    )

    assert result["error"]["code"] == "WORKSPACE_ACCESS_DENIED"
    assert tenant.canonical_name in result["error"]["message"]
    assert "t_secret_schema" not in str(result)


async def test_materialization_status_denies_a_thread_job_lookup_too(workspace, user, tenant):
    run = await _status_run(workspace, tenant)
    thread = await Thread.objects.acreate(workspace=workspace, user=user)
    job = await ThreadJob.objects.acreate(
        thread=thread,
        job_type=ThreadJob.JobType.MATERIALIZATION,
        procrastinate_job_id=run.procrastinate_job_id,
        tool_call_id="tc-status",
    )
    await TenantMembership.objects.filter(user=user, tenant=tenant).aupdate(
        archived_at=timezone.now()
    )

    result = await server.get_materialization_status(
        str(job.id), workspace_id=str(workspace.id), user_id=str(user.id)
    )

    assert result["error"]["code"] == "WORKSPACE_ACCESS_DENIED"
    assert "t_secret_schema" not in str(result)
