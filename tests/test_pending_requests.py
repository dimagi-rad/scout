"""Messages typed during a chat's first data load are held as one unsent turn.

No LLM turn runs while the load is queued; the load's resume (or the next live turn)
claims the held request under the thread's turn lease and sends it as one message.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.functions import Now
from django.test import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.agents.graph.base import human_turn_count
from apps.chat import pending_requests, turn_lease
from apps.chat.constants import MAX_MESSAGE_LENGTH, SYSTEM_RESUME_MARKER
from apps.chat.message_converter import langchain_messages_to_ui
from apps.chat.models import PendingRequest, Thread, ThreadJob
from apps.chat.turn_lease import atry_acquire_turn_lease
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
from apps.workspaces.tasks import (
    HELD_REQUEST_NOTE,
    NO_REQUEST_NOTE,
    resume_thread_after_materialization,
)
from tests.tenant_access import ausable_connection

User = get_user_model()


def _delete_jobs(job_ids):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [list(job_ids)])


@pytest.fixture
def queued_jobs():
    before = set(ProcrastinateJob.objects.values_list("id", flat=True))
    yield
    _delete_jobs(set(ProcrastinateJob.objects.values_list("id", flat=True)) - before)


class _AgentLayer:
    def __init__(self):
        self.inputs = []
        self.build = AsyncMock()

    async def stream(self, _agent, input_state, *_args, **_kwargs):
        self.inputs.append(input_state)
        yield 'data: {"type":"finish"}\n\n'


@pytest.fixture
def agent_layer():
    layer = _AgentLayer()
    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", new_callable=AsyncMock),
        patch("apps.chat.views.build_agent_graph", layer.build),
        patch(
            "apps.chat.views.repair_dangling_tool_calls", new_callable=AsyncMock, return_value=[]
        ),
        patch("apps.chat.views.langgraph_to_ui_stream", side_effect=layer.stream),
    ):
        yield layer


@pytest.fixture
def checkpoint():
    """The message ids the checkpoint holds; a claimed request lands when its id is added."""
    ids: set[str] = set()

    async def has_message(_thread_id, message_id):
        return message_id in ids

    with patch.object(pending_requests, "athread_has_message", side_effect=has_message):
        yield ids


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


async def _held_id(thread_id, version: int) -> str:
    pending = await PendingRequest.objects.aget(thread_id=thread_id)
    return pending_requests.held_message_id(pending.request_id, version)


async def _post(client, ws, thread_id, text, *, message_id=None, **data):
    message = {"role": "user", "parts": [{"type": "text", "text": text}]}
    if message_id:
        message["id"] = message_id
    return await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [message],
                "data": {"workspaceId": str(ws.id), "threadId": thread_id, **data},
            }
        ),
        content_type="application/json",
    )


async def _events(response) -> list[dict]:
    body = b"".join([chunk async for chunk in response.streaming_content]).decode()
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def _held_events(response) -> dict:
    assert response.status_code == 200, response.content
    events = await _events(response)
    [held] = [e for e in events if e["type"] == "data-pending-request"]
    assert held["transient"] is True
    assert events[-1]["type"] == "finish"
    return held["data"]


async def _loading_chat(slug, **member_kwargs):
    ws, tenant = await _workspace(slug)
    user, client = await _member(ws, tenant, f"chatter-{slug}@b.c", **member_kwargs)
    return ws, tenant, user, client


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("queued_jobs", "checkpoint")
class TestHold:
    async def test_the_first_message_is_held_for_the_load_it_starts(self, agent_layer):
        ws, _tenant, _user, client = await _loading_chat("first")
        thread_id = str(uuid.uuid4())

        held = await _held_events(await _post(client, ws, thread_id, "how many visits?"))

        agent_layer.build.assert_not_awaited()
        assert agent_layer.inputs == []
        pending = await PendingRequest.objects.aget(thread_id=thread_id)
        job = await ThreadJob.objects.aget(thread_id=thread_id)
        assert pending.thread_job_id == job.id
        assert [p["text"] for p in pending.parts] == ["how many visits?"]
        assert held["version"] == 1
        assert held["state"] == "waiting"
        assert held["thread_job_state"] == ThreadJob.State.PENDING
        assert await Thread.objects.filter(id=thread_id).aexists()

    async def test_later_messages_join_it_once_each(self, agent_layer):
        ws, _tenant, _user, client = await _loading_chat("later")
        thread_id = str(uuid.uuid4())
        await _held_events(await _post(client, ws, thread_id, "visits?", message_id="m1"))

        await _held_events(await _post(client, ws, thread_id, "by district", message_id="m2"))
        held = await _held_events(
            await _post(client, ws, thread_id, "by district", message_id="m2")
        )

        assert held["version"] == 2
        assert [p["id"] for p in held["parts"]] == ["m1", "m2"]
        assert agent_layer.inputs == []

    async def test_a_request_past_the_length_cap_is_refused(self, agent_layer):
        ws, _tenant, _user, client = await _loading_chat("long")
        thread_id = str(uuid.uuid4())
        await _held_events(await _post(client, ws, thread_id, "x" * (MAX_MESSAGE_LENGTH - 5)))

        response = await _post(client, ws, thread_id, "more words")

        assert response.status_code == 400
        assert response.json()["error"] == pending_requests.REQUEST_TOO_LONG_MESSAGE
        pending = await PendingRequest.objects.aget(thread_id=thread_id)
        assert len(pending.parts) == 1

    async def test_a_workspace_that_serves_data_answers_normally(self, agent_layer):
        ws, tenant, user, client = await _loading_chat("served")
        await TenantSchema.objects.acreate(
            tenant=tenant, schema_name="t_served", state=SchemaState.ACTIVE
        )
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        await ThreadJob.objects.acreate(
            thread=thread, job_type="materialization", procrastinate_job_id=91001
        )

        response = await _post(client, ws, str(thread.id), "refresh it")
        await _events(response)

        assert len(agent_layer.inputs) == 1
        assert not await PendingRequest.objects.filter(thread=thread).aexists()

    async def test_a_reader_with_no_load_of_their_own_gets_a_normal_turn(self, agent_layer):
        ws, _tenant, _user, client = await _loading_chat("reader", role=WorkspaceRole.READ)

        response = await _post(client, ws, str(uuid.uuid4()), "anything?")
        await _events(response)

        assert len(agent_layer.inputs) == 1
        assert not await PendingRequest.objects.aexists()

    async def test_a_thread_that_is_answering_is_not_held(self, agent_layer):
        ws, _tenant, _user, client = await _loading_chat("answering")
        thread_id = str(uuid.uuid4())
        await _held_events(await _post(client, ws, thread_id, "first"))
        assert await atry_acquire_turn_lease(thread_id) is not None

        with patch("apps.chat.views.TURN_LEASE_WAIT_SECONDS", 0):
            response = await _post(client, ws, thread_id, "second")

        assert response.status_code == 409
        assert response.json()["reason"] == "thread_busy"
        pending = await PendingRequest.objects.aget(thread_id=thread_id)
        assert len(pending.parts) == 1

    async def test_a_load_whose_resume_began_is_not_held_for(self, agent_layer):
        ws, _tenant, _user, client = await _loading_chat("resumed")
        thread_id = str(uuid.uuid4())
        await _held_events(await _post(client, ws, thread_id, "first"))
        await ThreadJob.objects.filter(thread_id=thread_id).aupdate(state=ThreadJob.State.RUNNING)

        held = await pending_requests.ahold_message(thread_id, part_id="m9", text="late")

        assert held is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("queued_jobs")
class TestLiveTurnSendsTheHeldRequest:
    async def _stranded(self, slug):
        """A held request whose load ended without sending it."""
        ws, _tenant, _user, client = await _loading_chat(slug)
        thread_id = str(uuid.uuid4())
        await _held_events(await _post(client, ws, thread_id, "visits?", message_id="m1"))
        await ThreadJob.objects.filter(thread_id=thread_id).aupdate(state=ThreadJob.State.FAILED)
        return ws, client, thread_id

    async def test_a_new_message_carries_the_held_text_first(self, agent_layer, checkpoint):
        ws, client, thread_id = await self._stranded("carry")

        await _events(await _post(client, ws, thread_id, "and by month"))

        [state] = agent_layer.inputs
        message = state["messages"][-1]
        assert message.content == "visits?\n\nand by month"
        assert message.id == await _held_id(thread_id, 1)
        # The mocked stream never wrote the checkpoint, so the request waits again.
        pending = await PendingRequest.objects.aget(thread_id=thread_id)
        assert pending.state == PendingRequest.State.WAITING
        assert pending.claim_token is None

    async def test_send_now_sends_the_shown_request_and_deletes_it_once_saved(
        self, agent_layer, checkpoint
    ):
        ws, client, thread_id = await self._stranded("sendnow")
        checkpoint.add(await _held_id(thread_id, 1))

        await _events(await _post(client, ws, thread_id, "visits?", pendingRequestVersion=1))

        [state] = agent_layer.inputs
        assert state["messages"][-1].content == "visits?"
        assert not await PendingRequest.objects.filter(thread_id=thread_id).aexists()

    async def test_send_now_against_an_older_version_is_refused(self, agent_layer, checkpoint):
        ws, client, thread_id = await self._stranded("stale-version")
        await pending_requests.aadd_part(thread_id, part_id="m2", text="more")

        response = await _post(client, ws, thread_id, "visits?", pendingRequestVersion=1)

        assert response.status_code == 409
        assert response.json()["reason"] == "version"
        assert agent_layer.inputs == []
        pending = await PendingRequest.objects.aget(thread_id=thread_id)
        assert pending.state == PendingRequest.State.WAITING
        assert await Thread.objects.filter(id=thread_id, turn_lease_token=None).aexists()

    async def test_send_now_of_another_request_at_the_same_version_is_refused(
        self, agent_layer, checkpoint
    ):
        ws, client, thread_id = await self._stranded("other-request")

        response = await _post(
            client,
            ws,
            thread_id,
            "visits?",
            pendingRequestVersion=1,
            pendingRequestId=str(uuid.uuid4()),
        )

        assert response.status_code == 409
        assert response.json()["reason"] == "version"
        assert agent_layer.inputs == []

    async def test_a_response_closed_unsent_returns_the_claim(self, agent_layer, checkpoint):
        ws, client, thread_id = await self._stranded("closed-unsent")

        response = await _post(client, ws, thread_id, "and by month")
        await sync_to_async(response.close)()

        pending = await PendingRequest.objects.aget(thread_id=thread_id)
        assert pending.state == PendingRequest.State.WAITING
        assert pending.claim_token is None

    async def test_a_claim_failure_does_not_fail_the_turn(self, agent_layer, checkpoint):
        ws, client, thread_id = await self._stranded("claim-broke")

        with patch.object(
            pending_requests, "aclaim", AsyncMock(side_effect=RuntimeError("db blip"))
        ):
            await _events(await _post(client, ws, thread_id, "and by month"))

        [state] = agent_layer.inputs
        assert state["messages"][-1].content == "and by month"

    async def test_send_now_after_it_was_sent_elsewhere_is_refused(self, agent_layer, checkpoint):
        ws, client, thread_id = await self._stranded("gone")
        await PendingRequest.objects.filter(thread_id=thread_id).adelete()

        response = await _post(client, ws, thread_id, "visits?", pendingRequestVersion=1)

        assert response.status_code == 409
        assert response.json()["reason"] == "gone"
        assert agent_layer.inputs == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestClaim:
    async def _held(self, slug):
        user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
        ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        job = await ThreadJob.objects.acreate(
            thread=thread,
            job_type="materialization",
            procrastinate_job_id=abs(hash(slug)) % 10**9,
        )
        await pending_requests.ahold_message(thread.id, part_id="m1", text="first")
        return thread, job

    async def test_an_add_after_the_claim_is_refused(self, checkpoint):
        thread, _job = await self._held("add-after-claim")
        lease = await atry_acquire_turn_lease(thread.id)
        claimed = await pending_requests.aclaim(thread.id, lease.token)

        with pytest.raises(pending_requests.PendingRequestConflict) as exc:
            await pending_requests.aadd_part(thread.id, part_id="m2", text="second")

        assert exc.value.reason == "claimed"
        assert claimed.text == "first"

    async def test_a_threads_next_request_gets_a_message_id_of_its_own(self, checkpoint):
        thread, job = await self._held("next-request")
        lease = await atry_acquire_turn_lease(thread.id)
        first = await pending_requests.aclaim(thread.id, lease.token)
        checkpoint.add(first.message_id)
        await pending_requests.asettle(first)
        await ThreadJob.objects.filter(id=job.id).aupdate(state=ThreadJob.State.COMPLETED)
        await lease.release()
        await ThreadJob.objects.acreate(
            thread=thread, job_type="materialization", procrastinate_job_id=987654
        )
        await pending_requests.ahold_message(thread.id, part_id="m9", text="again")
        lease = await atry_acquire_turn_lease(thread.id)

        second = await pending_requests.aclaim(thread.id, lease.token)

        assert second.version == first.version
        assert second.message_id != first.message_id
        # The first request's message in the checkpoint must not settle the second.
        await pending_requests.asettle(second)
        assert await PendingRequest.objects.filter(thread=thread).aexists()

    async def test_a_resume_claims_only_the_request_held_for_its_load(self, checkpoint):
        thread, job = await self._held("other-load")
        other = await ThreadJob.objects.acreate(
            thread=thread, job_type="materialization", procrastinate_job_id=123321
        )
        lease = await atry_acquire_turn_lease(thread.id)

        assert await pending_requests.aclaim(thread.id, lease.token, thread_job_id=other.id) is None
        claimed = await pending_requests.aclaim(thread.id, lease.token, thread_job_id=job.id)
        assert claimed.text == "first"

    async def test_a_request_takes_only_so_many_parts(self, checkpoint):
        thread, _job = await self._held("many-parts")
        for n in range(2, pending_requests.MAX_PARTS + 1):
            await pending_requests.aadd_part(thread.id, part_id=f"m{n}", text="x")

        with pytest.raises(pending_requests.PendingRequestTooLong) as exc:
            await pending_requests.aadd_part(thread.id, part_id="one-more", text="x")
        assert str(exc.value) == pending_requests.TOO_MANY_PARTS_MESSAGE
        with pytest.raises(pending_requests.PendingRequestTooLong):
            await pending_requests.ahold_message(thread.id, part_id="held-more", text="x")

    async def test_an_add_before_the_claim_goes_out_with_it(self, checkpoint):
        thread, _job = await self._held("add-before-claim")
        await pending_requests.aadd_part(thread.id, part_id="m2", text="second")
        lease = await atry_acquire_turn_lease(thread.id)

        claimed = await pending_requests.aclaim(thread.id, lease.token)

        assert claimed.text == "first\n\nsecond"
        assert claimed.version == 2

    async def test_only_the_lease_holder_claims(self, checkpoint):
        thread, _job = await self._held("not-holder")

        assert await pending_requests.aclaim(thread.id, uuid.uuid4()) is None

    async def test_a_stale_claim_that_was_sent_is_dropped_not_resent(self, checkpoint):
        thread, _job = await self._held("stale-sent")
        await PendingRequest.objects.filter(thread=thread).aupdate(
            state=PendingRequest.State.CLAIMED, claim_token=uuid.uuid4()
        )
        checkpoint.add(await _held_id(thread.id, 1))
        lease = await atry_acquire_turn_lease(thread.id)

        assert await pending_requests.aclaim(thread.id, lease.token) is None
        assert not await PendingRequest.objects.filter(thread=thread).aexists()

    async def test_a_stale_claim_that_never_landed_is_taken_over(self, checkpoint):
        thread, _job = await self._held("stale-unsent")
        await PendingRequest.objects.filter(thread=thread).aupdate(
            state=PendingRequest.State.CLAIMED, claim_token=uuid.uuid4()
        )
        lease = await atry_acquire_turn_lease(thread.id)

        claimed = await pending_requests.aclaim(thread.id, lease.token)

        assert claimed.text == "first"
        pending = await PendingRequest.objects.aget(thread=thread)
        assert pending.claim_token == lease.token

    async def test_a_stale_claim_shows_as_waiting_but_refuses_adds(self, checkpoint):
        thread, _job = await self._held("stale-shown")
        await PendingRequest.objects.filter(thread=thread).aupdate(
            state=PendingRequest.State.CLAIMED, claim_token=uuid.uuid4()
        )

        shown = await pending_requests.athread_pending_request(thread.id)

        assert shown["state"] == "waiting"
        with pytest.raises(pending_requests.PendingRequestConflict):
            await pending_requests.aadd_part(thread.id, part_id="m2", text="second")

    async def test_a_live_claim_shows_as_claimed(self, checkpoint):
        thread, _job = await self._held("live-shown")
        lease = await atry_acquire_turn_lease(thread.id)
        await pending_requests.aclaim(thread.id, lease.token)

        shown = await pending_requests.athread_pending_request(thread.id)

        assert shown["state"] == "claimed"

    async def test_a_claim_whose_lease_lapsed_shows_as_waiting(self, checkpoint):
        thread, _job = await self._held("lapsed-shown")
        lease = await atry_acquire_turn_lease(thread.id)
        await pending_requests.aclaim(thread.id, lease.token)
        await Thread.objects.filter(id=thread.id).aupdate(
            turn_lease_expires_at=Now() - turn_lease.TURN_LEASE_TTL
        )

        shown = await pending_requests.athread_pending_request(thread.id)

        assert shown["state"] == "waiting"


def _resume_agent(*, raises=None):
    agent = MagicMock()
    agent.ainvoke = AsyncMock(side_effect=raises, return_value={"messages": []})
    return agent


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestResume:
    async def _loaded(self, slug, run_state=MaterializationRun.RunState.COMPLETED):
        user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
        ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
        tenant = await Tenant.objects.acreate(
            external_id=f"t-{slug}", provider="commcare", canonical_name="Tenant"
        )
        await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
        schema = await TenantSchema.objects.acreate(tenant=tenant, schema_name=f"s_{slug}")
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        pj = abs(hash(slug)) % 10**9
        tj = await ThreadJob.objects.acreate(
            thread=thread, job_type="materialization", procrastinate_job_id=pj
        )
        await MaterializationRun.objects.acreate(
            tenant_schema=schema, pipeline="commcare_sync", state=run_state, procrastinate_job_id=pj
        )
        return thread, tj

    async def _resume(self, tj, agent):
        with patch("apps.workspaces.tasks._build_agent_for_resume", AsyncMock(return_value=agent)):
            return await resume_thread_after_materialization(None, thread_job_id=str(tj.id))

    async def test_the_held_request_follows_its_own_marker_message(self, checkpoint):
        thread, tj = await self._loaded("resume-held")
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")
        await pending_requests.aadd_part(thread.id, part_id="m2", text="by month")
        agent = _resume_agent()
        held_id = await _held_id(thread.id, 2)

        async def lands(*_args, **_kwargs):
            checkpoint.add(held_id)
            return {"messages": []}

        agent.ainvoke.side_effect = lands

        result = await self._resume(tj, agent)

        assert result["status"] == "resumed"
        marker, request = agent.ainvoke.await_args.args[0]["messages"]
        assert marker.content.startswith(SYSTEM_RESUME_MARKER)
        assert HELD_REQUEST_NOTE in marker.content
        assert "original request" not in marker.content
        assert marker.id == f"{held_id}-sys"
        assert request.content == "visits?\n\nby month"
        assert request.id == held_id
        assert not await PendingRequest.objects.filter(thread=thread).aexists()

    async def test_a_failed_load_still_answers_the_request(self, checkpoint):
        thread, tj = await self._loaded("resume-failed", MaterializationRun.RunState.FAILED)
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")
        agent = _resume_agent()

        await self._resume(tj, agent)

        marker, request = agent.ainvoke.await_args.args[0]["messages"]
        assert "FAILED" in marker.content
        assert marker.content.endswith(HELD_REQUEST_NOTE)
        assert request.content == "visits?"

    async def test_a_cancelled_load_still_answers_the_request(self, checkpoint):
        thread, tj = await self._loaded("resume-cancelled", MaterializationRun.RunState.CANCELLED)
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")
        await ThreadJob.objects.filter(id=tj.id).aupdate(state=ThreadJob.State.CANCELLED)
        agent = _resume_agent()

        await self._resume(tj, agent)

        _marker, request = agent.ainvoke.await_args.args[0]["messages"]
        assert request.content == "visits?"

    async def test_a_resume_that_fails_before_sending_leaves_it_waiting(self, checkpoint):
        thread, tj = await self._loaded("resume-broke")
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")

        with patch(
            "apps.workspaces.tasks._persist_synthetic_failure_message", new_callable=AsyncMock
        ):
            result = await self._resume(tj, _resume_agent(raises=RuntimeError("model down")))

        assert result["status"] == "agent_failed"
        pending = await PendingRequest.objects.aget(thread=thread)
        assert pending.state == PendingRequest.State.WAITING
        assert pending.claim_token is None
        shown = await pending_requests.athread_pending_request(thread.id)
        assert shown["thread_job_state"] == ThreadJob.State.FAILED

    async def test_a_resume_that_breaks_before_the_agent_leaves_it_waiting(self, checkpoint):
        thread, tj = await self._loaded("resume-early-break")
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")

        with (
            patch(
                "apps.workspaces.tasks._aggregate_materialization_state",
                AsyncMock(side_effect=RuntimeError("db blip")),
            ),
            pytest.raises(RuntimeError),
        ):
            await self._resume(tj, _resume_agent())

        pending = await PendingRequest.objects.aget(thread=thread)
        assert pending.state == PendingRequest.State.WAITING
        assert pending.claim_token is None

    async def test_a_thread_with_nothing_held_resumes_as_before(self, checkpoint):
        _thread, tj = await self._loaded("resume-legacy")
        agent = _resume_agent()

        with patch.object(pending_requests, "athread_has_user_turn", AsyncMock(return_value=True)):
            await self._resume(tj, agent)

        [message] = agent.ainvoke.await_args.args[0]["messages"]
        assert "continue with the user's original request" in message.content

    async def test_a_chat_whose_request_was_discarded_is_not_asked_to_continue_it(self, checkpoint):
        _thread, tj = await self._loaded("resume-discarded")
        agent = _resume_agent()

        with patch.object(pending_requests, "athread_has_user_turn", AsyncMock(return_value=False)):
            await self._resume(tj, agent)

        [message] = agent.ainvoke.await_args.args[0]["messages"]
        assert NO_REQUEST_NOTE in message.content
        assert "original request" not in message.content


def test_the_request_counts_as_one_human_turn_and_shows_as_one_bubble():
    marker = HumanMessage(content=f"{SYSTEM_RESUME_MARKER} loaded. {HELD_REQUEST_NOTE}", id="x-sys")
    request = HumanMessage(content="visits?\n\nby month", id="pr-t-2")
    messages = [marker, request, AIMessage(content="42 visits")]

    assert human_turn_count(messages) == 1
    ui = langchain_messages_to_ui(messages)
    assert [(m["role"], m["id"]) for m in ui] == [("user", "pr-t-2"), ("assistant", ui[1]["id"])]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("queued_jobs", "checkpoint")
class TestEndpoints:
    async def _held(self, slug, agent_layer):
        ws, _tenant, _user, client = await _loading_chat(slug)
        thread_id = str(uuid.uuid4())
        await _held_events(await _post(client, ws, thread_id, "visits?", message_id="m1"))
        base = f"/api/workspaces/{ws.id}/threads/{thread_id}"
        return ws, client, thread_id, base

    async def test_add_part_is_idempotent_on_its_id(self, agent_layer):
        _ws, client, _thread_id, base = await self._held("ep-add", agent_layer)

        for _ in range(2):
            response = await client.post(
                f"{base}/pending-request/parts/",
                data=json.dumps({"id": "m2", "text": "by month"}),
                content_type="application/json",
            )
            assert response.status_code == 200

        assert response.json()["version"] == 2
        assert [p["text"] for p in response.json()["parts"]] == ["visits?", "by month"]

    async def test_add_part_to_a_claimed_request_is_a_conflict(self, agent_layer):
        _ws, client, thread_id, base = await self._held("ep-claimed", agent_layer)
        lease = await atry_acquire_turn_lease(thread_id)
        await pending_requests.aclaim(thread_id, lease.token)

        response = await client.post(
            f"{base}/pending-request/parts/",
            data=json.dumps({"id": "m2", "text": "by month"}),
            content_type="application/json",
        )

        assert response.status_code == 409
        assert response.json()["reason"] == "claimed"

    async def test_add_part_with_nothing_held_is_a_conflict(self, agent_layer):
        _ws, client, thread_id, base = await self._held("ep-gone", agent_layer)
        await PendingRequest.objects.filter(thread_id=thread_id).adelete()

        response = await client.post(
            f"{base}/pending-request/parts/",
            data=json.dumps({"id": "m2", "text": "by month"}),
            content_type="application/json",
        )

        assert response.status_code == 409
        assert response.json()["reason"] == "gone"

    async def test_another_users_thread_is_not_found(self, agent_layer):
        ws, _client, _thread_id, base = await self._held("ep-foreign", agent_layer)
        tenant = await Tenant.objects.aget(external_id="t-ep-foreign")
        _other, intruder = await _member(ws, tenant, "other-ep@b.c")

        response = await intruder.post(
            f"{base}/pending-request/parts/",
            data=json.dumps({"id": "m2", "text": "mine now"}),
            content_type="application/json",
        )

        assert response.status_code == 404

    async def test_discard_needs_the_current_version(self, agent_layer):
        _ws, client, thread_id, base = await self._held("ep-discard", agent_layer)

        stale = await client.delete(
            f"{base}/pending-request/",
            data=json.dumps({"version": 7}),
            content_type="application/json",
        )
        current = await client.delete(
            f"{base}/pending-request/",
            data=json.dumps({"version": 1}),
            content_type="application/json",
        )

        assert stale.status_code == 409
        assert stale.json()["reason"] == "version"
        assert current.status_code == 200
        assert not await PendingRequest.objects.filter(thread_id=thread_id).aexists()

    async def test_messages_and_the_jobs_poll_include_it(self, agent_layer):
        ws, client, thread_id, base = await self._held("ep-read", agent_layer)

        plain = await client.get(f"{base}/messages/")
        with_pending = await client.get(f"{base}/messages/?include=pending")
        jobs = await client.get(f"/api/workspaces/{ws.id}/jobs/active/")

        assert plain.json() == []
        assert with_pending.json()["messages"] == []
        assert with_pending.json()["pending_request"]["version"] == 1
        assert jobs.json()["pending_requests"][thread_id]["parts"][0]["text"] == "visits?"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_messages_include_pending_for_a_thread_not_yet_created():
    ws, _tenant, _user, client = await _loading_chat("fresh")

    response = await client.get(
        f"/api/workspaces/{ws.id}/threads/{uuid.uuid4()}/messages/?include=pending"
    )

    assert response.json() == {"messages": [], "pending_request": None}
