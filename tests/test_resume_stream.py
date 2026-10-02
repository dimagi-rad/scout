"""Streaming a background resume's answer to open chats (apps/chat/resume_stream.py)."""

from datetime import timedelta
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from apps.chat import resume_stream
from apps.chat.models import ResumeStreamChunk, Thread
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole
from tests.test_pending_requests import _loading_chat, _member

User = get_user_model()
ANSWER = "There were forty-two visits last week, most of them in Kisumu."


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _graph(answer: str = ANSWER, *, fails: bool = False):
    model = GenericFakeChatModel(messages=iter([AIMessage(content=answer)]))

    async def agent(state: _State) -> dict:
        reply = await model.ainvoke(state["messages"])
        if fails:
            raise RuntimeError("model down")
        return {"messages": [reply]}

    graph = StateGraph(_State)
    graph.add_node("agent", agent)
    graph.add_edge(START, "agent")
    graph.add_edge("agent", END)
    return graph.compile()


async def _thread(slug):
    user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    return ws, user, thread


async def _rows(thread):
    return [row async for row in ResumeStreamChunk.objects.filter(thread=thread).order_by("id")]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.real_resume_stream
class TestRunStreamed:
    async def test_it_returns_the_final_state_and_streams_the_answer(self):
        _ws, _user, thread = await _thread("stream")

        with patch.object(resume_stream, "FLUSH_CHARS", 10):
            final = await resume_stream.arun_streamed(
                _graph(), {"messages": [HumanMessage("visits?")]}, {}, thread.id
            )

        assert final["messages"][-1].content == ANSWER
        rows = await _rows(thread)
        assert len(rows) > 2
        assert "".join(row.text for row in rows) == ANSWER
        assert [row.done for row in rows] == [False] * (len(rows) - 1) + [True]
        assert len({row.run for row in rows}) == 1

    async def test_a_failing_run_still_ends_its_stream_and_raises(self):
        _ws, _user, thread = await _thread("stream-fail")

        with pytest.raises(RuntimeError, match="model down"):
            await resume_stream.arun_streamed(
                _graph(fails=True), {"messages": [HumanMessage("visits?")]}, {}, thread.id
            )

        rows = await _rows(thread)
        assert rows[-1].done is True

    async def test_a_write_that_fails_never_fails_the_run(self):
        _ws, _user, thread = await _thread("stream-broken")

        with patch.object(
            ResumeStreamChunk.objects, "acreate", AsyncMock(side_effect=RuntimeError("db"))
        ):
            final = await resume_stream.arun_streamed(
                _graph(), {"messages": [HumanMessage("visits?")]}, {}, thread.id
            )

        assert final["messages"][-1].content == ANSWER


def test_only_answer_text_is_streamed():
    assert resume_stream.chunk_text(AIMessage(content="plain")) == "plain"
    blocks = [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "visible"},
        {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
    ]
    assert resume_stream.chunk_text(AIMessage(content=blocks)) == "visible"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTailEndpoint:
    async def _chat(self, slug):
        ws, tenant, user, client = await _loading_chat(slug)
        thread = await Thread.objects.acreate(workspace=ws, user=user)
        return ws, tenant, thread, client

    async def test_it_returns_what_streamed_after_an_id(self):
        ws, _tenant, thread, client = await self._chat("tail")
        first = await ResumeStreamChunk.objects.acreate(thread=thread, run=thread.id, text="a")
        await ResumeStreamChunk.objects.acreate(thread=thread, run=thread.id, text="b", done=True)
        url = f"/api/workspaces/{ws.id}/threads/{thread.id}/resume-stream/"

        everything = (await client.get(url)).json()["chunks"]
        later = (await client.get(f"{url}?after={first.id}")).json()["chunks"]

        assert [c["text"] for c in everything] == ["a", "b"]
        assert [(c["text"], c["done"]) for c in later] == [("b", True)]

    async def test_another_users_thread_shows_nothing(self):
        ws, tenant, thread, _client = await self._chat("tail-owner")
        await ResumeStreamChunk.objects.acreate(thread=thread, run=thread.id, text="secret")
        _other, intruder = await _member(ws, tenant, "tail-other@b.c")

        response = await intruder.get(f"/api/workspaces/{ws.id}/threads/{thread.id}/resume-stream/")

        assert response.json() == {"chunks": []}

    async def test_a_bad_cursor_is_refused(self):
        ws, _tenant, thread, client = await self._chat("tail-bad")

        response = await client.get(
            f"/api/workspaces/{ws.id}/threads/{thread.id}/resume-stream/?after=x"
        )

        assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_old_streams_are_pruned():
    _ws, _user, thread = await _thread("prune")
    old = await ResumeStreamChunk.objects.acreate(thread=thread, run=thread.id, text="old")
    await ResumeStreamChunk.objects.filter(id=old.id).aupdate(
        created_at=timezone.now() - timedelta(hours=1)
    )
    await ResumeStreamChunk.objects.acreate(thread=thread, run=thread.id, text="new")

    assert await resume_stream.aprune() == 1
    assert [row.text for row in await _rows(thread)] == ["new"]
