"""Chat continuation: what a background turn leaves behind once a load ends.

A resume after materialization, the held request it answers, and the workspace
flush all run the real path through one ``FakeAgent``. These pin outcomes a
user or the chat can see (the conversation, the streamed text, job and request
state, a free thread), not where the code doing it lives, so they hold across a
move of that code.
"""

import asyncio
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage

from apps.chat import pending_requests
from apps.chat.constants import SYSTEM_RESUME_MARKER
from apps.chat.models import PendingRequest, Thread, ThreadJob
from apps.chat.resume_stream import aread_after
from apps.chat.stream import langgraph_to_ui_stream
from apps.chat.turn_lease import atry_acquire_turn_lease
from apps.workspaces.tasks import (
    FLUSH_FAILED_MESSAGE,
    FLUSH_NOTE,
    HELD_REQUEST_NOTE,
    RESUME_EXCEPTION_MESSAGE,
    RESUME_TIMEOUT_MESSAGE,
    flush_pending_requests,
    resume_thread_after_materialization,
)
from tests.agent_doubles import DEFAULT_REPLY, FakeAgent, serving
from tests.test_pending_requests import (  # noqa: F401 (queued_jobs registers the fixture)
    _held_id,
    _hold_without_load,
    _thread,
    queued_jobs,
)
from tests.test_resume_thread_task import _make_thread_job_ready_to_resume

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("queued_jobs"),
]


async def _resumable(slug: str, pj_id: int):
    _user, ws, thread, tj = await _make_thread_job_ready_to_resume(
        email=f"{slug}@b.c",
        ws_name=f"W-{slug}",
        ext_id=f"t-{slug}",
        schema_name=f"s_{slug}",
        pj_id=pj_id,
        tool_call=f"tc-{slug}",
    )
    return ws, thread, tj


async def _resume(agent: FakeAgent, tj) -> dict:
    with serving(agent):
        return await resume_thread_after_materialization(None, thread_job_id=str(tj.id))


async def _streamed(thread) -> tuple[str, list[bool]]:
    rows = await aread_after(thread.id, 0)
    return "".join(row["text"] for row in rows), [row["done"] for row in rows]


async def _thread_is_free(thread) -> bool:
    return await atry_acquire_turn_lease(thread.id) is not None


class TestResume:
    async def test_the_answer_is_streamed_and_saved_and_the_job_completes(self):
        _ws, thread, tj = await _resumable("cont-done", 930001)
        agent = FakeAgent()

        result = await _resume(agent, tj)

        assert result == {"status": "resumed", "terminal_state": ThreadJob.State.COMPLETED}
        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.COMPLETED
        marker, answer = await agent.thread_messages(thread.id)
        assert isinstance(marker, HumanMessage)
        assert marker.content.startswith(SYSTEM_RESUME_MARKER)
        assert isinstance(answer, AIMessage)
        assert answer.content == DEFAULT_REPLY
        text, done = await _streamed(thread)
        assert text == DEFAULT_REPLY
        assert done[-1] is True
        assert await _thread_is_free(thread)

    async def test_a_stop_during_the_turn_keeps_the_job_cancelled_and_the_answer(self):
        _ws, thread, tj = await _resumable("cont-stop", 930002)

        async def stop(_state):
            await ThreadJob.objects.filter(id=tj.id).aupdate(state=ThreadJob.State.CANCELLED)

        agent = FakeAgent(during=stop)

        result = await _resume(agent, tj)

        assert result["terminal_state"] == ThreadJob.State.CANCELLED
        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.CANCELLED
        assert (await agent.thread_messages(thread.id))[-1].content == DEFAULT_REPLY
        assert await _thread_is_free(thread)

    @override_settings(AGENT_RESUME_TIMEOUT_S=1)
    async def test_a_turn_cut_off_by_the_timeout_fails_with_a_message_and_ends_its_stream(self):
        _ws, thread, tj = await _resumable("cont-timeout", 930003)

        async def stall(_state):
            await asyncio.Event().wait()

        agent = FakeAgent(during=stall)

        result = await _resume(agent, tj)

        assert result == {"status": "agent_timeout"}
        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.FAILED
        assert tj.failure_phase == ThreadJob.FailurePhase.RESUME
        last = (await agent.thread_messages(thread.id))[-1]
        assert isinstance(last, AIMessage)
        assert last.content == RESUME_TIMEOUT_MESSAGE
        _text, done = await _streamed(thread)
        assert done == [True]
        assert await _thread_is_free(thread)

    async def test_a_failed_turn_fails_the_job_with_a_message(self):
        _ws, thread, tj = await _resumable("cont-fail", 930004)

        async def model_down(_state):
            raise RuntimeError("model down")

        agent = FakeAgent(during=model_down)

        result = await _resume(agent, tj)

        assert result == {"status": "agent_failed"}
        await tj.arefresh_from_db()
        assert tj.state == ThreadJob.State.FAILED
        assert (await agent.thread_messages(thread.id))[-1].content == RESUME_EXCEPTION_MESSAGE
        assert await _thread_is_free(thread)


class TestHeldRequest:
    async def test_the_resume_lands_the_held_request_behind_its_marker_and_answers_it(self):
        _ws, thread, tj = await _resumable("cont-held", 930005)
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")
        await pending_requests.aadd_part(thread.id, part_id="m2", text="by month")
        held_id = await _held_id(thread.id, 2)
        agent = FakeAgent()

        result = await _resume(agent, tj)

        assert result["status"] == "resumed"
        marker, request, answer = await agent.thread_messages(thread.id)
        assert marker.id == f"{held_id}-sys"
        assert HELD_REQUEST_NOTE in marker.content
        assert request.id == held_id
        assert request.content == "visits?\n\nby month"
        assert answer.content == DEFAULT_REPLY
        assert not await PendingRequest.objects.filter(thread=thread).aexists()
        assert (await _streamed(thread))[0] == DEFAULT_REPLY

    async def test_a_turn_that_failed_before_the_request_landed_leaves_it_waiting(self):
        _ws, thread, tj = await _resumable("cont-held-unsent", 930007)
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")
        agent = FakeAgent(fails_to_start=RuntimeError("no connection"))

        result = await _resume(agent, tj)

        assert result == {"status": "agent_failed"}
        assert [m.content for m in await agent.thread_messages(thread.id)] == [
            RESUME_EXCEPTION_MESSAGE
        ]
        pending = await PendingRequest.objects.aget(thread=thread)
        assert pending.state == PendingRequest.State.WAITING
        assert pending.claim_token is None
        assert await _thread_is_free(thread)

    async def test_a_request_that_landed_before_the_turn_failed_is_not_held_again(self):
        _ws, thread, tj = await _resumable("cont-held-fail", 930006)
        await pending_requests.ahold_message(thread.id, part_id="m1", text="visits?")
        held_id = await _held_id(thread.id, 1)

        async def model_down(_state):
            raise RuntimeError("model down")

        agent = FakeAgent(during=model_down)

        await _resume(agent, tj)

        messages = await agent.thread_messages(thread.id)
        assert [m.id for m in messages[:2]] == [f"{held_id}-sys", held_id]
        assert messages[-1].content == RESUME_EXCEPTION_MESSAGE
        assert not await PendingRequest.objects.filter(thread=thread).aexists()


class TestFlush:
    async def test_held_requests_are_answered_oldest_first_each_in_its_own_thread(self):
        ws, user, _client, older = await _thread("cont-flush")
        newer = await Thread.objects.acreate(workspace=ws, user=user)
        await _hold_without_load(newer, "newer?")
        await _hold_without_load(older, "older?")
        await PendingRequest.objects.filter(thread=older).aupdate(
            created_at=timezone.now() - timedelta(hours=1)
        )
        agent = FakeAgent()

        with serving(agent):
            result = await flush_pending_requests(str(ws.id))

        assert result == {"status": "flushed", "sent": 2}
        assert [run.messages[1].content for run in agent.runs] == ["older?", "newer?"]
        for thread, text in ((older, "older?"), (newer, "newer?")):
            marker, request, answer = await agent.thread_messages(thread.id)
            assert marker.content == FLUSH_NOTE
            assert marker.id == f"{request.id}-sys"
            assert request.content == text
            assert answer.content == DEFAULT_REPLY
            assert (await _streamed(thread))[0] == DEFAULT_REPLY
            assert await _thread_is_free(thread)
        assert not await PendingRequest.objects.filter(thread__workspace=ws).aexists()

    async def test_a_reply_that_failed_after_the_request_landed_says_so(self):
        ws, _user, _client, thread = await _thread("cont-flush-fail")
        await _hold_without_load(thread, "visits?")
        agent = FakeAgent(fails_after_reply=RuntimeError("model down"))

        with serving(agent):
            result = await flush_pending_requests(str(ws.id))

        assert result == {"status": "flushed", "sent": 1}
        _marker, request, apology = await agent.thread_messages(thread.id)
        assert request.content == "visits?"
        assert apology.content == FLUSH_FAILED_MESSAGE
        text, done = await _streamed(thread)
        assert text == DEFAULT_REPLY
        assert done[-1] is True
        assert not await PendingRequest.objects.filter(thread=thread).aexists()


async def test_the_same_double_drives_a_live_turn():
    agent = FakeAgent()
    config = {"configurable": {"thread_id": "live-turn"}}

    sse = "".join(
        [
            chunk
            async for chunk in langgraph_to_ui_stream(
                agent, {"messages": [HumanMessage("visits?")]}, config
            )
        ]
    )

    assert '"type":"text-delta"' in sse.replace(" ", "")
    assert "Kisumu" in sse
    assert (await agent.thread_messages("live-turn"))[-1].content == DEFAULT_REPLY
