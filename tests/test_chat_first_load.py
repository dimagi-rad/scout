"""A chat in a workspace with no data starts its load as the chatting user (#408)."""

import json
import re
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import AsyncClient
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.agents.graph.base import (
    _INTERACTIVE_MATERIALIZE_IN_PROGRESS_GUIDANCE,
    _LOAD_STARTED_FOR_THIS_CHAT_GUIDANCE,
    _build_system_prompt,
    _fetch_semantic_model_context,
)
from apps.chat.models import Thread, ThreadJob
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services import load_activity, reconciliation, thread_job_dispatch
from apps.workspaces.services.load_activity import MATERIALIZE_TASK_NAME, athread_awaits_load
from apps.workspaces.services.thread_job_dispatch import astart_chat_load
from apps.workspaces.tasks import materialize_workspace
from tests.tenant_access import ausable_connection

User = get_user_model()


# The one place the prompt may name the promise: the rule allowing it only when a
# tool result or the Data Availability section says so.
_CONDITIONAL_PROMISE = "says this conversation will resume automatically"
_PROMISE = re.compile(
    r"resume automatically|system will resume|resumes this conversation|"
    r"I'll continue|continue when it finishes|pick this up",
    re.IGNORECASE,
)


def _promises(prompt):
    return _PROMISE.findall(prompt.replace(_CONDITIONAL_PROMISE, ""))


def _delete_jobs(job_ids):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [list(job_ids)])


@pytest.fixture
def queued_jobs():
    """Real queue rows, removed afterwards: procrastinate_jobs is unmanaged, so the
    transactional test flush would leave them behind."""
    before = set(ProcrastinateJob.objects.values_list("id", flat=True))
    yield
    _delete_jobs(set(ProcrastinateJob.objects.values_list("id", flat=True)) - before)


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


async def _workspace(slug):
    owner = await User.objects.acreate_user(email=f"owner-{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=owner)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name=f"Tenant {slug}"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=owner, role=WorkspaceRole.MANAGE)
    return ws, tenant


async def _member(ws, tenant, email, role=WorkspaceRole.READ_WRITE):
    user = await User.objects.acreate_user(email=email, password="x")
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=role)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    client = AsyncClient()
    await client.alogin(email=email, password="x")
    return user, client


async def _chat(client, ws, thread_id=None):
    thread_id = thread_id or str(uuid.uuid4())
    resp = await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [{"role": "user", "content": "how many visits?"}],
                "workspaceId": str(ws.id),
                "threadId": thread_id,
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    _ = [chunk async for chunk in resp.streaming_content]
    return thread_id


async def _load_jobs(ws):
    return [
        job
        async for job in ProcrastinateJob.objects.filter(
            task_name=MATERIALIZE_TASK_NAME, args__workspace_id=str(ws.id)
        )
    ]


def test_the_queue_lookup_names_the_real_task():
    assert materialize_workspace.name == MATERIALIZE_TASK_NAME


def test_the_queue_lookup_treats_stalled_jobs_as_the_worker_janitor_does():
    stalled = reconciliation.MATERIALIZATION_STALLED_HEARTBEAT_SECONDS
    assert load_activity._STALLED_AFTER.total_seconds() == stalled
    assert set(load_activity._QUEUED_OR_RUNNING) == reconciliation._PROCRASTINATE_INFLIGHT_STATUSES
    assert list(load_activity._STARTED) == reconciliation._STARTED_JOB_STATUSES


@pytest.mark.asyncio
async def test_a_synthetic_conversation_id_awaits_no_load():
    assert await athread_awaits_load("recipe-run-7") is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("agent_layer", "queued_jobs")
class TestChatStartsTheLoad:
    async def test_first_chat_queues_one_load_as_the_chatter_bound_to_the_thread(self):
        ws, tenant = await _workspace("first")
        chatter, client = await _member(ws, tenant, "chatter-first@b.c")

        thread_id = await _chat(client, ws)

        jobs = await _load_jobs(ws)
        assert len(jobs) == 1
        assert jobs[0].args["user_id"] == str(chatter.id)
        assert jobs[0].args["load_intent"] == {str(tenant.id): 1}
        assert jobs[0].args["only_unserved"] is True
        assert jobs[0].scheduled_at is not None
        tj = await ThreadJob.objects.aget(procrastinate_job_id=jobs[0].id)
        assert str(tj.thread_id) == thread_id
        assert tj.job_type == ThreadJob.JobType.MATERIALIZATION
        assert tj.state == ThreadJob.State.PENDING

    async def test_a_second_chat_does_not_queue_another_load(self):
        ws, tenant = await _workspace("second")
        _, first = await _member(ws, tenant, "first-second@b.c")
        _, second = await _member(ws, tenant, "second-second@b.c")

        thread_id = await _chat(first, ws)
        await _chat(first, ws, thread_id)
        other_thread = await _chat(second, ws)

        assert len(await _load_jobs(ws)) == 1
        assert not await ThreadJob.objects.filter(thread_id=other_thread).aexists()

    async def test_a_chat_whose_load_failed_is_not_reloaded_on_its_next_message(self):
        ws, tenant = await _workspace("failed")
        _, client = await _member(ws, tenant, "chatter-failed@b.c")
        thread_id = await _chat(client, ws)
        [job] = await _load_jobs(ws)
        await ThreadJob.objects.filter(procrastinate_job_id=job.id).aupdate(
            state=ThreadJob.State.FAILED
        )
        await sync_to_async(_delete_jobs)([job.id])

        await _chat(client, ws, thread_id)

        assert await _load_jobs(ws) == []

    async def test_a_load_queued_at_create_is_not_doubled(self):
        ws, tenant = await _workspace("create")
        _, client = await _member(ws, tenant, "chatter-create@b.c")
        await materialize_workspace.defer_async(
            workspace_id=str(ws.id), user_id="", only_unserved=True, notify_thread=False
        )

        await _chat(client, ws)

        assert len(await _load_jobs(ws)) == 1

    async def test_a_load_already_running_is_not_doubled(self):
        ws, tenant = await _workspace("running")
        _, client = await _member(ws, tenant, "chatter-running@b.c")
        schema = await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="t_running", state=SchemaState.PROVISIONING
        )
        await MaterializationRun.objects.acreate(
            tenant_schema=schema,
            pipeline="commcare_sync",
            state=MaterializationRun.RunState.LOADING,
        )

        await _chat(client, ws)

        assert await _load_jobs(ws) == []

    async def test_a_loaded_workspace_queues_nothing(self):
        ws, tenant = await _workspace("loaded")
        _, client = await _member(ws, tenant, "chatter-loaded@b.c")
        await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="t_loaded", state=SchemaState.ACTIVE
        )

        await _chat(client, ws)

        assert await _load_jobs(ws) == []

    async def test_a_partly_served_workspace_is_left_to_the_agent(self):
        ws, tenant = await _workspace("partial")
        _, client = await _member(ws, tenant, "chatter-partial@b.c")
        other = await Tenant.objects.acreate(
            external_id="t-partial-2", provider="commcare", canonical_name="Partial Two"
        )
        await WorkspaceTenant.objects.acreate(workspace=ws, tenant=other)
        await TenantMembership.objects.acreate(
            user=await User.objects.aget(email="chatter-partial@b.c"),
            tenant=other,
            connection=await ausable_connection(
                await User.objects.aget(email="chatter-partial@b.c"), other.provider
            ),
        )
        await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="t_partial", state=SchemaState.ACTIVE
        )

        await _chat(client, ws)

        assert await _load_jobs(ws) == []

    async def test_a_failing_preflight_does_not_fail_the_chat(self):
        ws, tenant = await _workspace("preflight")
        _, client = await _member(ws, tenant, "chatter-preflight@b.c")

        with patch.object(
            thread_job_dispatch,
            "aunserved_tenant_ids",
            AsyncMock(side_effect=RuntimeError("db blip")),
        ):
            await _chat(client, ws)

        assert await _load_jobs(ws) == []

    async def test_a_read_only_member_queues_nothing(self):
        ws, tenant = await _workspace("reader")
        _, client = await _member(ws, tenant, "reader-reader@b.c", role=WorkspaceRole.READ)

        await _chat(client, ws)

        assert await _load_jobs(ws) == []

    async def test_two_chats_racing_past_the_pending_check_queue_one_load(self):
        ws, tenant = await _workspace("race")
        user, _ = await _member(ws, tenant, "chatter-race@b.c")
        threads = [await Thread.objects.acreate(workspace=ws, user=user) for _ in range(2)]

        with patch.object(
            thread_job_dispatch, "aworkspace_load_pending", AsyncMock(return_value=False)
        ):
            first = await astart_chat_load(workspace=ws, user=user, thread_id=threads[0].id)
            second = await astart_chat_load(workspace=ws, user=user, thread_id=threads[1].id)

        assert first is not None
        assert second is None
        assert len(await _load_jobs(ws)) == 1
        assert not await ThreadJob.objects.filter(thread=threads[1]).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("queued_jobs")
class TestPromptWhileTheLoadIsQueued:
    async def test_the_chat_it_is_bound_to_is_told_it_will_be_resumed(self):
        ws, tenant = await _workspace("bound")
        user, _ = await _member(ws, tenant, "chatter-bound@b.c")
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        assert await astart_chat_load(workspace=ws, user=user, thread_id=thread.id)

        context = await _fetch_semantic_model_context(
            ws, write_capable=True, conversation_id=str(thread.id)
        )

        assert _LOAD_STARTED_FOR_THIS_CHAT_GUIDANCE in context
        assert "No data has been loaded yet" not in context

    async def test_data_already_serving_is_not_hidden_behind_a_queued_load(self):
        ws, tenant = await _workspace("serving")
        await _member(ws, tenant, "chatter-serving@b.c")
        await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="t_serving", state=SchemaState.ACTIVE
        )
        await materialize_workspace.defer_async(
            workspace_id=str(ws.id), user_id="", notify_thread=False
        )

        context = await _fetch_semantic_model_context(ws, write_capable=True)

        assert _INTERACTIVE_MATERIALIZE_IN_PROGRESS_GUIDANCE not in context
        assert "Data is loaded" in context

    async def test_another_chat_is_told_a_load_is_in_progress(self):
        ws, tenant = await _workspace("other")
        user, _ = await _member(ws, tenant, "chatter-other@b.c")
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        await materialize_workspace.defer_async(
            workspace_id=str(ws.id), user_id="", notify_thread=False
        )

        context = await _fetch_semantic_model_context(
            ws, write_capable=True, conversation_id=str(thread.id)
        )

        assert _INTERACTIVE_MATERIALIZE_IN_PROGRESS_GUIDANCE in context
        assert "No data has been loaded yet" not in context


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("queued_jobs")
class TestFullPromptPromisesAResumeOnlyWhenBound:
    """An unbound load's chat must not read a resume promise anywhere in its prompt,
    and the bound chat must keep its own."""

    @pytest.mark.parametrize("write_capable", [True, False])
    async def test_a_chat_with_no_bound_load_is_told_nothing_resumes_it(self, write_capable):
        ws, tenant = await _workspace(f"orphan-{write_capable}")
        role = WorkspaceRole.READ_WRITE if write_capable else WorkspaceRole.READ
        user, _ = await _member(ws, tenant, f"chatter-orphan-{write_capable}@b.c", role)
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        await materialize_workspace.defer_async(
            workspace_id=str(ws.id), user_id="", notify_thread=False
        )

        stable, volatile = await _build_system_prompt(
            ws, user, write_capable=write_capable, conversation_id=str(thread.id)
        )

        assert _promises(stable + volatile) == []
        assert "Nothing will resume this conversation" in volatile
        assert "without naming a time" in volatile

    async def test_the_chat_a_load_is_bound_to_is_promised_a_resume(self):
        ws, tenant = await _workspace("bound-full")
        user, _ = await _member(ws, tenant, "chatter-bound-full@b.c")
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        assert await astart_chat_load(workspace=ws, user=user, thread_id=thread.id)

        stable, volatile = await _build_system_prompt(
            ws, user, write_capable=True, conversation_id=str(thread.id)
        )

        assert _promises(stable) == []
        assert _promises(volatile) == ["resume automatically"]
        assert "Nothing will resume this conversation" not in volatile
