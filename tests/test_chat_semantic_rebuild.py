"""Opening a chat on loaded data with no usable catalog rebuilds it, without a reload (#714).

The agent used to be told to "run materialization" for this: a full reload that
the base prompt makes it ask the user to approve, only to rebuild a data model
that needed no new data.
"""

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import AsyncClient
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.agents.graph import base as graph_base
from apps.agents.graph.base import _fetch_semantic_model_context
from apps.semantic.models import CubeSchema, SemanticModel
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceDataRecovery,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.tasks import recover_workspace_data
from tests.tenant_access import ausable_connection

User = get_user_model()
SEMANTIC_REBUILD = WorkspaceDataRecovery.RecoveryType.SEMANTIC_REBUILD


@pytest.fixture
def queued_jobs():
    """procrastinate_jobs is unmanaged, so the transactional flush would leave rows behind."""
    before = set(ProcrastinateJob.objects.values_list("id", flat=True))
    yield
    added = set(ProcrastinateJob.objects.values_list("id", flat=True)) - before
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [list(added)])


@pytest.fixture
def agent_layer():
    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", new_callable=AsyncMock),
        patch("apps.chat.views.build_agent_graph", new_callable=AsyncMock),
        patch(
            "apps.chat.views.repair_dangling_tool_calls", new_callable=AsyncMock, return_value=[]
        ),
        patch("apps.chat.views.langgraph_to_ui_stream", side_effect=_no_stream),
    ):
        yield


async def _no_stream(*_args, **_kwargs):
    for chunk in ():
        yield chunk


async def _loaded_workspace(slug, *, synced_at=None):
    """A single-source workspace serving data from a completed load, with no catalog."""
    owner = await User.objects.acreate_user(email=f"owner-{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=owner)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name=f"Tenant {slug}"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=owner, role=WorkspaceRole.MANAGE)
    schema = await TenantSchema.objects.acreate(
        tenant=tenant, schema_name=f"t_{slug}", state=SchemaState.ACTIVE
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=synced_at or timezone.now() - timedelta(hours=1),
    )
    return ws, tenant, schema


async def _member(ws, tenant, email, role=WorkspaceRole.READ_WRITE):
    user = await User.objects.acreate_user(email=email, password="x")
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=role)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    client = AsyncClient()
    await client.alogin(email=email, password="x")
    return user, client


async def _chat(client, ws):
    resp = await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [{"role": "user", "content": "make me a dashboard"}],
                "workspaceId": str(ws.id),
                "threadId": str(uuid.uuid4()),
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _ = [chunk async for chunk in resp.streaming_content]


async def _rebuilds(ws):
    return [r async for r in WorkspaceDataRecovery.objects.filter(workspace=ws)]


async def _stale_catalog(ws):
    model = await SemanticModel.objects.acreate(
        workspace=ws,
        name="Stale",
        status=SemanticModel.Status.ACTIVE,
        metadata={"last_build": {"ok": False, "error": "Cube rejected schema"}},
    )
    await CubeSchema.objects.acreate(
        workspace=ws,
        semantic_model=model,
        status=CubeSchema.Status.ACTIVE,
        filename="stale.yaml",
        content="cubes: []",
        content_hash="stale",
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("catalog", ["missing", "last_build_failed"])
async def test_a_writer_opening_a_chat_queues_one_semantic_rebuild(
    agent_layer, queued_jobs, catalog
):
    ws, tenant, _schema = await _loaded_workspace(f"rebuild-{catalog}")
    if catalog == "last_build_failed":
        await _stale_catalog(ws)
    user, client = await _member(ws, tenant, f"writer-{catalog}@b.c")

    await _chat(client, ws)
    await _chat(client, ws)

    (recovery,) = await _rebuilds(ws)
    assert recovery.recovery_type == SEMANTIC_REBUILD
    assert recovery.state == WorkspaceDataRecovery.State.PENDING
    assert recovery.requested_by_id == user.id
    assert recovery.source_id is None
    job = await ProcrastinateJob.objects.aget(id=recovery.procrastinate_job_id)
    assert job.task_name == recover_workspace_data.name
    assert job.args == {"recovery_id": str(recovery.id)}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_read_only_member_never_queues_a_rebuild(agent_layer, queued_jobs):
    ws, tenant, _schema = await _loaded_workspace("reader")
    _user, client = await _member(ws, tenant, "reader@b.c", role=WorkspaceRole.READ)

    await _chat(client, ws)

    assert await _rebuilds(ws) == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_load_in_flight_builds_the_catalog_itself(agent_layer, queued_jobs):
    ws, tenant, schema = await _loaded_workspace("loading")
    await MaterializationRun.objects.acreate(
        tenant_schema=schema, pipeline="commcare_sync", state=MaterializationRun.RunState.LOADING
    )
    _user, client = await _member(ws, tenant, "loading@b.c")

    await _chat(client, ws)

    assert await _rebuilds(ws) == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_one_attempt_per_sync_so_a_failing_build_is_not_retried_every_message(
    agent_layer, queued_jobs
):
    ws, tenant, schema = await _loaded_workspace(
        "attempted", synced_at=timezone.now() - timedelta(hours=2)
    )
    user, client = await _member(ws, tenant, "attempted@b.c")
    await WorkspaceDataRecovery.objects.acreate(
        workspace=ws,
        requested_by=user,
        recovery_type=SEMANTIC_REBUILD,
        source_type="chat",
        state=WorkspaceDataRecovery.State.FAILED,
    )

    await _chat(client, ws)
    assert len(await _rebuilds(ws)) == 1

    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.COMPLETED,
        completed_at=timezone.now(),
    )
    await _chat(client, ws)
    assert len(await _rebuilds(ws)) == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_the_recovery_rebuilds_a_serving_catalog_whose_last_build_failed():
    ws, tenant, _schema = await _loaded_workspace("stale-worker")
    await _stale_catalog(ws)
    user, _client = await _member(ws, tenant, "stale-worker@b.c")
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=ws, requested_by=user, recovery_type=SEMANTIC_REBUILD, source_type="chat"
    )

    async def rebuild(_workspace_id):
        await SemanticModel.objects.filter(workspace=ws).aupdate(
            metadata={"last_build": {"ok": True}}
        )
        return {"cube_schema": {"ok": True}}

    with patch(
        "apps.workspaces.tasks.rebuild_workspace_semantic_model_core",
        AsyncMock(side_effect=rebuild),
    ) as rebuild_core:
        result = await recover_workspace_data.func(
            SimpleNamespace(job=SimpleNamespace(id=915)), str(recovery.id)
        )

    rebuild_core.assert_awaited_once_with(str(ws.id))
    assert result["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rebuilding", [True, False])
async def test_the_agent_is_never_sent_to_a_reload_for_the_data_model(rebuilding):
    ws, _tenant, _schema = await _loaded_workspace(f"prompt-{rebuilding}")
    if rebuilding:
        await WorkspaceDataRecovery.objects.acreate(
            workspace=ws, recovery_type=SEMANTIC_REBUILD, source_type="chat"
        )

    context = await _fetch_semantic_model_context(ws, interactive=True, write_capable=True)

    assert "Run materialization to rebuild" not in context
    assert "Do NOT call `run_materialization`" in context
    assert "approve a reload" in context
    expected = (
        graph_base._SEMANTIC_REBUILDING_GUIDANCE
        if rebuilding
        else graph_base._SEMANTIC_REBUILD_NOT_RUNNING_GUIDANCE
    )
    assert expected in context
    assert ("check back" in context) is rebuilding


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_another_members_failed_attempt_does_not_block_this_one(agent_layer, queued_jobs):
    """That attempt may have failed on the other member's own access."""
    ws, tenant, _schema = await _loaded_workspace("other-failed")
    other, _other_client = await _member(ws, tenant, "other-failed@b.c")
    await WorkspaceDataRecovery.objects.acreate(
        workspace=ws,
        requested_by=other,
        recovery_type=SEMANTIC_REBUILD,
        source_type="chat",
        state=WorkspaceDataRecovery.State.FAILED,
    )
    _user, client = await _member(ws, tenant, "this-member@b.c")

    await _chat(client, ws)

    assert len(await _rebuilds(ws)) == 2


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_chat_recovery_never_turns_into_an_unapproved_reload():
    """The snapshot can go unsafe between the chat opening and the job running."""
    ws, tenant, schema = await _loaded_workspace("went-unsafe")
    user, _client = await _member(ws, tenant, "went-unsafe@b.c")
    recovery = await WorkspaceDataRecovery.objects.acreate(
        workspace=ws, requested_by=user, recovery_type=SEMANTIC_REBUILD, source_type="chat"
    )
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.FAILED,
        completed_at=timezone.now(),
    )

    with patch("apps.workspaces.tasks.materialize_workspace_core", AsyncMock()) as reload:
        result = await recover_workspace_data.func(
            SimpleNamespace(job=SimpleNamespace(id=916)), str(recovery.id)
        )

    reload.assert_not_awaited()
    assert result["status"] == "failed"
    await recovery.arefresh_from_db()
    assert "needs a data refresh" in recovery.error


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_unsafe_snapshot_still_points_the_agent_at_a_refresh():
    ws, _tenant, schema = await _loaded_workspace("prompt-unsafe")
    await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.FAILED,
        completed_at=timezone.now(),
    )

    context = await _fetch_semantic_model_context(ws, interactive=True, write_capable=True)

    assert graph_base._LOADED_NEEDS_RELOAD_GUIDANCE in context
    assert "being rebuilt" not in context


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_reloading_recovery_is_not_described_as_a_rebuild():
    ws, _tenant, _schema = await _loaded_workspace("prompt-reloading")
    await WorkspaceDataRecovery.objects.acreate(
        workspace=ws, recovery_type=WorkspaceDataRecovery.RecoveryType.MATERIALIZATION
    )

    context = await _fetch_semantic_model_context(ws, interactive=True, write_capable=True)

    assert graph_base._INTERACTIVE_MATERIALIZE_IN_PROGRESS_GUIDANCE in context
    assert "reloads nothing" not in context
