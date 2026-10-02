"""Generated thread titles: one title on the Thread row, replaced once after a turn."""

import json
import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from django.test import AsyncClient
from langchain_core.messages import AIMessage
from procrastinate.contrib.django.models import ProcrastinateJob

from apps.chat import stream, titles
from apps.chat.models import Thread
from apps.chat.tasks import TITLE_JOB_PRIORITY, aschedule_thread_title, generate_thread_title
from apps.chat.titles import agenerate_thread_title, clean_generated_title


@pytest.fixture
def queued_jobs():
    """procrastinate_jobs is unmanaged, so the transactional flush would leave rows behind."""
    before = set(ProcrastinateJob.objects.values_list("id", flat=True))
    yield
    added = set(ProcrastinateJob.objects.values_list("id", flat=True)) - before
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [list(added)])


class _FakeTitleModel:
    """Stands in for ChatAnthropic; records the prompt and returns a canned reply."""

    calls: list = []
    reply: str | BaseException = "Module completion rates"
    during_call = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ainvoke(self, messages, config=None):
        type(self).calls.append({"kwargs": self.kwargs, "messages": messages, "config": config})
        if type(self).during_call is not None:
            await type(self).during_call()
        if isinstance(type(self).reply, BaseException):
            raise type(self).reply
        return AIMessage(content=type(self).reply)


@pytest.fixture
def title_model(monkeypatch):
    _FakeTitleModel.calls = []
    _FakeTitleModel.reply = "Module completion rates"
    _FakeTitleModel.during_call = None
    monkeypatch.setattr(titles, "ChatAnthropic", _FakeTitleModel)
    return _FakeTitleModel


async def _thread(workspace, user, **fields):
    defaults = {"title": "What are the module completion rates by district?"}
    return await Thread.objects.acreate(workspace=workspace, user=user, **{**defaults, **fields})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Module completion rates."', "Module completion rates"),
        ("Title: Visits by worker", "Visits by worker"),
        ("**Payment reconciliation**\nExplanation follows", "Payment reconciliation"),
        ("Why are visits down?", "Why are visits down"),
        (
            "one two three four five six seven eight nine ten",
            "one two three four five six seven eight",
        ),
        ("  \n ", ""),
        ('""', ""),
    ],
)
def test_clean_generated_title(raw, expected):
    assert clean_generated_title(raw) == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generates_title_from_first_message_with_haiku(workspace, user, title_model):
    thread = await _thread(workspace, user)
    updated_at = thread.updated_at

    assert await agenerate_thread_title(str(thread.id)) == "generated"

    await thread.arefresh_from_db()
    assert thread.title == "Module completion rates"
    assert thread.title_source == Thread.TitleSource.GENERATED
    assert thread.title_is_custom is False
    assert thread.updated_at == updated_at
    (call,) = title_model.calls
    assert call["kwargs"]["model"] == "claude-haiku-4-5-20251001"
    assert "What are the module completion rates by district?" in call["messages"][-1].content


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_generation_is_idempotent(workspace, user, title_model):
    thread = await _thread(workspace, user)

    assert await agenerate_thread_title(str(thread.id)) == "generated"
    assert await agenerate_thread_title(str(thread.id)) == "skipped"

    assert len(title_model.calls) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"title": "My renamed chat", "title_is_custom": True, "title_source": "user"},
        # A row whose flags disagree still counts as renamed.
        {"title": "My renamed chat", "title_is_custom": True},
    ],
)
async def test_never_overwrites_a_renamed_title(workspace, user, title_model, fields):
    thread = await _thread(workspace, user, **fields)

    assert await agenerate_thread_title(str(thread.id)) == "skipped"

    await thread.arefresh_from_db()
    assert thread.title == "My renamed chat"
    assert title_model.calls == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_rename_during_generation_wins(workspace, user, title_model):
    thread = await _thread(workspace, user)

    async def rename():
        await Thread.objects.filter(id=thread.id).aupdate(
            title="Renamed meanwhile", title_is_custom=True, title_source="user"
        )

    title_model.during_call = rename

    assert await agenerate_thread_title(str(thread.id)) == "skipped"

    await thread.arefresh_from_db()
    assert thread.title == "Renamed meanwhile"
    assert thread.title_source == Thread.TitleSource.USER


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [RuntimeError("anthropic down"), "  "])
async def test_failure_keeps_the_provisional_title_and_logs(
    workspace, user, title_model, caplog, reply
):
    thread = await _thread(workspace, user)
    title_model.reply = reply

    with caplog.at_level(logging.WARNING, logger="apps.chat.titles"):
        assert await agenerate_thread_title(str(thread.id)) == "failed"

    await thread.arefresh_from_db()
    assert thread.title == "What are the module completion rates by district?"
    assert thread.title_source == Thread.TitleSource.FIRST_MESSAGE
    assert str(thread.id) in caplog.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_untitled_thread_falls_back_to_the_checkpoint(
    workspace, user, title_model, monkeypatch
):
    thread = await _thread(workspace, user, title="")
    monkeypatch.setattr(
        titles, "_afirst_user_message", AsyncMock(return_value="Show visits by worker")
    )

    assert await agenerate_thread_title(str(thread.id)) == "generated"

    assert "Show visits by worker" in title_model.calls[0]["messages"][-1].content


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_missing_thread_is_a_no_op(title_model):
    assert await agenerate_thread_title(str(uuid.uuid4())) == "missing"
    assert title_model.calls == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_schedule_queues_one_prioritized_job_per_thread(workspace, user, queued_jobs):
    thread = await _thread(workspace, user)

    await aschedule_thread_title(thread)
    await aschedule_thread_title(thread)

    jobs = [
        job
        async for job in ProcrastinateJob.objects.filter(
            task_name=generate_thread_title.name, args={"thread_id": str(thread.id)}
        )
    ]
    assert len(jobs) == 1
    assert jobs[0].priority == TITLE_JOB_PRIORITY
    assert jobs[0].queueing_lock == f"thread-title-{thread.id}"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["generated", "user"])
async def test_schedule_skips_settled_titles(workspace, user, queued_jobs, source):
    thread = await _thread(workspace, user, title_source=source)

    await aschedule_thread_title(thread)

    assert not await ProcrastinateJob.objects.filter(
        task_name=generate_thread_title.name, args={"thread_id": str(thread.id)}
    ).aexists()


class _Agent:
    def __init__(self, exc: BaseException | None = None):
        self._exc = exc

    def astream_events(self, input_state, *, config, version):
        exc = self._exc

        async def _gen():
            if exc is not None:
                raise exc
            yield {"event": "on_chain_end", "name": "agent", "data": {}}

        return _gen()


@pytest.mark.asyncio
async def test_stream_runs_on_success_before_finish():
    order = []

    async def on_success():
        order.append("hook")

    async for chunk in stream.langgraph_to_ui_stream(
        _Agent(), {}, {"configurable": {}}, on_success=on_success
    ):
        if '"finish"' in chunk:
            order.append("finish")

    assert order == ["hook", "finish"]


@pytest.mark.asyncio
async def test_stream_skips_on_success_after_an_error():
    on_success = AsyncMock()

    chunks = [
        chunk
        async for chunk in stream.langgraph_to_ui_stream(
            _Agent(RuntimeError("boom")), {}, {"configurable": {}}, on_success=on_success
        )
    ]

    assert any('"error"' in chunk for chunk in chunks)
    on_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_on_success_does_not_break_the_stream():
    chunks = [
        chunk
        async for chunk in stream.langgraph_to_ui_stream(
            _Agent(),
            {},
            {"configurable": {}},
            on_success=AsyncMock(side_effect=RuntimeError("queue down")),
        )
    ]

    assert '"finish"' in chunks[-1]


async def _succeeding_stream(*_args, on_success=None, **_kwargs):
    await on_success()
    yield 'data: {"type": "finish"}\n\n'


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_successful_chat_turn_queues_title_generation(workspace, user, queued_jobs):
    client = AsyncClient()
    await sync_to_async(client.force_login)(user)
    thread_id = str(uuid.uuid4())

    async def chat():
        response = await client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "Visits by worker last month"}],
                    "workspaceId": str(workspace.id),
                    "threadId": thread_id,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        _ = [chunk async for chunk in response.streaming_content]

    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", new_callable=AsyncMock),
        patch("apps.chat.views.build_agent_graph", new_callable=AsyncMock),
        patch("apps.chat.views.astart_chat_load", new_callable=AsyncMock, return_value=object()),
        patch("apps.chat.views.touch_workspace_schemas", new_callable=AsyncMock),
        patch(
            "apps.chat.views.repair_dangling_tool_calls", new_callable=AsyncMock, return_value=[]
        ),
        patch("apps.chat.views.langgraph_to_ui_stream", side_effect=_succeeding_stream),
    ):
        await chat()
        thread = await Thread.objects.aget(id=thread_id)
        assert thread.title == "Visits by worker last month"
        assert thread.title_source == Thread.TitleSource.FIRST_MESSAGE
        assert await ProcrastinateJob.objects.filter(
            task_name=generate_thread_title.name, args={"thread_id": thread_id}
        ).aexists()

        await Thread.objects.filter(id=thread_id).aupdate(title_source="generated")
        await ProcrastinateJob.objects.filter(task_name=generate_thread_title.name).adelete()
        await chat()
        assert not await ProcrastinateJob.objects.filter(
            task_name=generate_thread_title.name, args={"thread_id": thread_id}
        ).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_first_turn_titles_a_row_created_before_any_message(workspace, user, queued_jobs):
    # The canvas creates the row before the first chat message, with no title.
    shell = await Thread.objects.acreate(workspace=workspace, user=user)
    client = AsyncClient()
    await sync_to_async(client.force_login)(user)

    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", new_callable=AsyncMock),
        patch("apps.chat.views.build_agent_graph", new_callable=AsyncMock),
        patch("apps.chat.views.astart_chat_load", new_callable=AsyncMock, return_value=object()),
        patch("apps.chat.views.touch_workspace_schemas", new_callable=AsyncMock),
        patch(
            "apps.chat.views.repair_dangling_tool_calls", new_callable=AsyncMock, return_value=[]
        ),
        patch("apps.chat.views.langgraph_to_ui_stream", side_effect=_succeeding_stream),
    ):
        response = await client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "Visits by worker last month"}],
                    "workspaceId": str(workspace.id),
                    "threadId": str(shell.id),
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        _ = [chunk async for chunk in response.streaming_content]

    await shell.arefresh_from_db()
    assert shell.title == "Visits by worker last month"
    assert shell.title_source == Thread.TitleSource.FIRST_MESSAGE
