"""Streaming a background resume's answer to open chats (apps/chat/resume_stream.py)."""

import logging
import uuid
from datetime import timedelta
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from anthropic import APITimeoutError
from django.contrib.auth import get_user_model
from django.utils import timezone
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from apps.chat import pending_requests, resume_stream
from apps.chat.models import ResumeStreamChunk, Thread
from apps.chat.services import continuation
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole
from tests.agent_doubles import DEFAULT_REPLY, FakeAgent
from tests.test_pending_requests import _loading_chat, _member

User = get_user_model()


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _graph_with_subagent(lead: str = ""):
    """A tool call to a subagent graph tagged as the app's managers tag theirs."""

    def nested():
        model = GenericFakeChatModel(messages=iter([AIMessage(content="SUBAGENT PROSE")]))

        async def agent(state: _State) -> dict:
            return {"messages": [await model.ainvoke(state["messages"])]}

        graph = StateGraph(_State)
        graph.add_node("agent", agent)
        graph.add_edge(START, "agent")
        graph.add_edge("agent", END)
        return graph.compile()

    @tool
    async def manager(task: str) -> str:
        """Run the subagent."""
        async for _event in nested().astream_events(
            {"messages": [HumanMessage(task)]},
            # As the app's managers do: a run of their own, tagged as a subagent's.
            config={
                "configurable": {"thread_id": "subagent-run"},
                "tags": ["subagent", "m"],
                "metadata": {"subagent": "m"},
            },
            version="v2",
        ):
            pass
        return "done"

    replies = [AIMessage(content=lead)] if lead else []
    model = GenericFakeChatModel(messages=iter([*replies, AIMessage(content="MAIN ANSWER")]))
    call = {"tool": {"name": "manager", "args": {"task": "t"}, "id": "c1"}}

    async def agent(state: _State) -> dict:
        if len(state["messages"]) == 1:
            # The tool call is attached by hand: the fake model streams content only.
            reply = await model.ainvoke(state["messages"]) if lead else AIMessage(content="")
            return {
                "messages": [
                    AIMessage(content=reply.content, id=reply.id, tool_calls=[call["tool"]])
                ]
            }
        return {"messages": [await model.ainvoke(state["messages"])]}

    def route(state: _State):
        return "tools" if state["messages"][-1].tool_calls else END

    graph = StateGraph(_State)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([manager]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


async def _thread(slug):
    user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    return ws, user, thread


def _config(thread):
    return {"configurable": {"thread_id": str(thread.id)}}


async def _rows(thread):
    return [row async for row in ResumeStreamChunk.objects.filter(thread=thread).order_by("id")]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestRunStreamed:
    async def test_it_returns_the_final_state_and_streams_the_answer(self):
        _ws, _user, thread = await _thread("stream")

        with patch.object(resume_stream, "FLUSH_CHARS", 10):
            final = await resume_stream.arun_streamed(
                FakeAgent(), {"messages": [HumanMessage("visits?")]}, _config(thread), thread.id
            )

        assert final["messages"][-1].content == DEFAULT_REPLY
        rows = await _rows(thread)
        assert len(rows) > 2
        assert "".join(row.text for row in rows) == DEFAULT_REPLY
        assert [row.done for row in rows] == [False] * (len(rows) - 1) + [True]
        assert len({row.run for row in rows}) == 1

    async def test_a_failing_run_still_ends_its_stream_and_raises(self):
        _ws, _user, thread = await _thread("stream-fail")

        with pytest.raises(RuntimeError, match="model down"):
            await resume_stream.arun_streamed(
                FakeAgent(fails_after_reply=RuntimeError("model down")),
                {"messages": [HumanMessage("visits?")]},
                _config(thread),
                thread.id,
            )

        rows = await _rows(thread)
        assert "".join(row.text for row in rows) == DEFAULT_REPLY
        assert rows[-1].done is True

    async def test_a_write_that_fails_never_fails_the_run(self):
        _ws, _user, thread = await _thread("stream-broken")

        with patch.object(
            ResumeStreamChunk.objects, "acreate", AsyncMock(side_effect=RuntimeError("db"))
        ):
            final = await resume_stream.arun_streamed(
                FakeAgent(), {"messages": [HumanMessage("visits?")]}, _config(thread), thread.id
            )

        assert final["messages"][-1].content == DEFAULT_REPLY

    async def test_a_subagents_own_answer_is_not_streamed(self):
        _ws, _user, thread = await _thread("stream-subagent")

        final = await resume_stream.arun_streamed(
            _graph_with_subagent(), {"messages": [HumanMessage("visits?")]}, {}, thread.id
        )

        assert final["messages"][-1].content == "MAIN ANSWER"
        streamed = "".join(row.text for row in await _rows(thread))
        assert streamed == "MAIN ANSWER"

    async def test_separate_model_calls_are_kept_apart(self):
        _ws, _user, thread = await _thread("stream-calls")

        await resume_stream.arun_streamed(
            _graph_with_subagent(lead="Let me check."),
            {"messages": [HumanMessage("visits?")]},
            {},
            thread.id,
        )

        streamed = "".join(row.text for row in await _rows(thread))
        assert streamed == "Let me check.\n\nMAIN ANSWER"

    async def test_the_flush_streams_its_answer(self):
        _ws, _user, thread = await _thread("stream-flush")
        thread = await Thread.objects.select_related("workspace", "user").aget(id=thread.id)
        held = pending_requests.ClaimedRequest(
            thread_id=str(thread.id),
            request_id=uuid.uuid4(),
            version=1,
            text="visits?",
            token=uuid.uuid4(),
        )

        with patch(
            "apps.chat.services.continuation.build_agent_for_resume",
            AsyncMock(return_value=FakeAgent()),
        ):
            await continuation._answer_flushed_request(thread, held)

        rows = await _rows(thread)
        assert "".join(row.text for row in rows) == DEFAULT_REPLY
        assert rows[-1].done is True

    async def test_a_run_whose_writes_failed_still_ends_its_stream(self):
        _ws, _user, thread = await _thread("stream-gap")
        writer = resume_stream.ResumeStreamWriter(thread.id)
        real = ResumeStreamChunk.objects.acreate
        calls = {"n": 0}

        async def fails_once(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db blip")
            return await real(**kwargs)

        with patch.object(ResumeStreamChunk.objects, "acreate", side_effect=fails_once):
            await writer.feed("x" * resume_stream.FLUSH_CHARS)
            await writer.feed("more text")
            await writer.close()

        [row] = await _rows(thread)
        assert row.done is True
        assert row.text == ""


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

    async def test_a_first_read_starts_at_the_latest_run(self):
        ws, _tenant, thread, client = await self._chat("tail-latest")
        old, new = uuid.uuid4(), uuid.uuid4()
        await ResumeStreamChunk.objects.acreate(thread=thread, run=old, text="old answer")
        await ResumeStreamChunk.objects.acreate(thread=thread, run=old, text="", done=True)
        await ResumeStreamChunk.objects.acreate(thread=thread, run=new, text="new")

        chunks = (
            await client.get(f"/api/workspaces/{ws.id}/threads/{thread.id}/resume-stream/")
        ).json()["chunks"]

        assert [c["text"] for c in chunks] == ["new"]

    async def test_another_users_thread_shows_nothing(self):
        ws, tenant, thread, _client = await self._chat("tail-owner")
        await ResumeStreamChunk.objects.acreate(thread=thread, run=thread.id, text="secret")
        _other, intruder = await _member(ws, tenant, "tail-other@b.c")

        response = await intruder.get(f"/api/workspaces/{ws.id}/threads/{thread.id}/resume-stream/")

        assert response.json() == {"chunks": []}

    async def test_a_negative_cursor_reads_as_a_first_read(self):
        ws, _tenant, thread, client = await self._chat("tail-negative")
        old, new = uuid.uuid4(), uuid.uuid4()
        await ResumeStreamChunk.objects.acreate(thread=thread, run=old, text="old", done=True)
        await ResumeStreamChunk.objects.acreate(thread=thread, run=new, text="new")

        body = (
            await client.get(f"/api/workspaces/{ws.id}/threads/{thread.id}/resume-stream/?after=-1")
        ).json()

        assert [c["text"] for c in body["chunks"]] == ["new"]
        assert body["more"] is False

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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_flush_model_timeout_fails_the_flush_without_paging(caplog):
    _ws, _user, thread = await _thread("flush-timeout")
    thread = await Thread.objects.select_related("workspace", "user").aget(id=thread.id)
    held = pending_requests.ClaimedRequest(
        thread_id=str(thread.id),
        request_id=uuid.uuid4(),
        version=1,
        text="visits?",
        token=uuid.uuid4(),
    )
    timeout = APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com"))
    agent = FakeAgent(during=AsyncMock(side_effect=timeout))

    with (
        patch(
            "apps.chat.services.continuation.build_agent_for_resume", AsyncMock(return_value=agent)
        ),
        caplog.at_level(logging.WARNING, logger="apps.chat.services.continuation"),
    ):
        answered = await continuation._answer_flushed_request(thread, held)

    assert answered is False
    flush_logs = [r for r in caplog.records if r.name == "apps.chat.services.continuation"]
    assert any("model request timed out" in r.getMessage() for r in flush_logs)
    assert all(r.levelno < logging.ERROR for r in flush_logs)
