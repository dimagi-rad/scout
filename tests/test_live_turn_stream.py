"""A chat turn's text, written for chats that did not start it (#856)."""

import asyncio
import json
import threading
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db.models.query import QuerySet

from apps.chat import resume_stream
from apps.chat.models import ResumeStreamChunk, Thread
from apps.chat.resume_stream import LiveTurnWriter
from tests.test_thread_turn_lease import _agent_layer, _chat_member, _post_chat

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


def _part(**part) -> str:
    return f"data: {json.dumps(part)}\n\n"


def _delta(text: str) -> str:
    return _part(type="text-delta", id="text-0", delta=text)


async def _rows(thread_id):
    return [
        row async for row in ResumeStreamChunk.objects.filter(thread_id=thread_id).order_by("id")
    ]


async def _thread(slug: str):
    _ws, thread, _client = await _chat_member(slug)
    return thread


async def test_text_is_written_at_most_once_per_interval():
    thread = await _thread("live-batch")
    writer = LiveTurnWriter(thread.id)
    tokens = [f"t{i} " for i in range(150)]

    # 150 tokens over ~3s, about a model's streaming rate.
    started = time.monotonic()
    for token in tokens:
        writer.observe(_delta(token))
        await asyncio.sleep(0.02)
    await writer.close()
    elapsed = time.monotonic() - started

    rows = await _rows(thread.id)
    # One row per interval that had text, plus the done row with whatever the last
    # interval had not written.
    assert 2 <= len(rows) <= elapsed / resume_stream.LIVE_FLUSH_INTERVAL_SECONDS + 2
    assert [row.done for row in rows] == [False] * (len(rows) - 1) + [True]
    assert "".join(row.text for row in rows) == "".join(tokens)
    assert {row.run for row in rows} == {writer.run}
    assert writer.rows_written == len(rows)


async def test_each_model_call_is_a_run_ended_when_its_tool_starts():
    thread = await _thread("live-calls")
    writer = LiveTurnWriter(thread.id)

    with patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01):
        writer.observe(_delta("Let me check."))
        writer.observe(_part(type="text-end", id="text-0"))
        writer.observe(_part(type="tool-input-available", toolCallId="t1", toolName="q", input={}))
        await asyncio.sleep(0.05)

        # The first call is in the checkpoint by now: a chat loading the thread while
        # the tool runs finds its run ended, and so skips it.
        latest = await resume_stream.aread_after(thread.id, 0)
        assert [(chunk["text"], chunk["done"]) for chunk in latest] == [("Let me check.", True)]

        writer.observe(_part(type="tool-output-available", toolCallId="t1", output="rows"))
        writer.observe(_delta("Here it is."))
        await asyncio.sleep(0.05)
        # Mid-call, a new reader tails only the call still being written.
        assert [chunk["text"] for chunk in await resume_stream.aread_after(thread.id, 0)] == [
            "Here it is."
        ]

        writer.observe(_delta(" Visits rose."))
        await writer.close()

    rows = await _rows(thread.id)
    assert [(row.text, row.done) for row in rows] == [
        ("Let me check.", True),
        ("Here it is.", False),
        (" Visits rose.", True),
    ]
    assert rows[0].run != rows[1].run == rows[2].run == writer.run


async def test_a_flush_spanning_two_calls_writes_a_row_for_each():
    thread = await _thread("live-calls-one-flush")
    writer = LiveTurnWriter(thread.id)
    written = []
    real_acreate = ResumeStreamChunk.objects.acreate

    async def acreate(**fields):
        written.append((fields["text"], fields["done"]))
        return await real_acreate(**fields)

    with patch.object(ResumeStreamChunk.objects, "acreate", side_effect=acreate):
        writer.observe(_delta("Let me check."))
        writer.observe(_part(type="tool-output-available", toolCallId="t1", output="rows"))
        writer.observe(_delta("Here it is."))
        await writer.close()

    assert written == [("Let me check.", True), ("Here it is.", True)]


async def test_a_turn_ending_on_a_tool_still_writes_its_end():
    thread = await _thread("live-ends-on-tool")
    writer = LiveTurnWriter(thread.id)

    with patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01):
        writer.observe(_delta("Loading your data."))
        writer.observe(_part(type="tool-input-available", toolCallId="t1", toolName="q", input={}))
        await asyncio.sleep(0.05)
        await writer.close()

    # The second done row is the turn's end, for a chat that reloaded on the first.
    rows = await _rows(thread.id)
    assert [(row.text, row.done) for row in rows] == [("Loading your data.", True), ("", True)]


async def test_text_after_a_text_part_ends_is_the_next_call():
    thread = await _thread("live-retry")
    writer = LiveTurnWriter(thread.id)

    writer.observe(_delta("Cut off"))
    writer.observe(_part(type="text-end", id="text-0"))
    # A retried reply after rejected tool calls: no tool part between.
    writer.observe(_part(type="text-start", id="text-1"))
    writer.observe(_delta("Retried."))
    writer.observe(_part(type="text-end", id="text-1"))
    # The next call's thinking comes before its text.
    writer.observe(_part(type="reasoning-start", id="r"))
    writer.observe(_part(type="reasoning-delta", id="r", delta="thinking"))
    writer.observe(_delta("Next call."))
    await writer.close()

    rows = await _rows(thread.id)
    assert [(row.text, row.done) for row in rows] == [
        ("Cut off", True),
        ("Retried.", True),
        ("Next call.", True),
    ]
    assert len({row.run for row in rows}) == 3


async def test_closing_clears_earlier_turns_text_only():
    thread = await _thread("live-cleanup")
    with patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01):
        earlier = LiveTurnWriter(thread.id)
        earlier.observe(_delta("An earlier answer."))
        await asyncio.sleep(0.05)
        earlier.observe(_delta(" More."))
        await earlier.close()

        writer = LiveTurnWriter(thread.id)
        writer.observe(_delta("This answer."))
        await asyncio.sleep(0.05)
        # A run that took the thread once this one let go of it, before its close.
        later = await ResumeStreamChunk.objects.acreate(
            thread_id=thread.id, run=uuid.uuid4(), text="A newer turn.", done=False
        )
        writer.observe(_delta(" Done."))
        await writer.close()

    rows = await _rows(thread.id)
    # The earlier turn keeps its done row (pruned later). This turn keeps all of its
    # text, for a chat that reads it late, and the newer run is left alone.
    assert [(row.text, row.done) for row in rows] == [
        (" More.", True),
        ("This answer.", False),
        ("A newer turn.", False),
        (" Done.", True),
    ]
    assert later.id in {row.id for row in rows}


async def test_only_answer_text_is_written():
    thread = await _thread("live-parts")
    writer = LiveTurnWriter(thread.id)

    writer.observe(_part(type="start"))
    writer.observe(_part(type="reasoning-delta", id="r", delta="thinking"))
    writer.observe(_part(type="data-chat-status", data={"kind": "retryable-error"}))
    writer.observe("data: not json\n\n")
    writer.observe(_part(type="tool-output-available", toolCallId="t1", output="text-delta"))
    await writer.close()

    assert await _rows(thread.id) == []
    assert writer.rows_written == 0


async def test_a_failed_write_never_raises_and_still_tries_the_done_row():
    thread = await _thread("live-broken")
    writer = LiveTurnWriter(thread.id)
    failing = AsyncMock(side_effect=RuntimeError("db"))

    with (
        patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01),
        patch.object(ResumeStreamChunk.objects, "acreate", failing),
    ):
        writer.observe(_delta("partial"))
        await asyncio.sleep(0.05)
        writer.observe(_delta(" more"))
        await writer.close()

    # One failed text write stops the rest; the done row is still tried.
    assert failing.await_count == 2
    assert failing.await_args.kwargs == {
        "thread_id": thread.id,
        "run": writer.run,
        "text": "",
        "done": True,
    }


async def test_a_turn_streams_its_text_and_clears_it_once_done():
    ws, thread, client = await _chat_member("live-turn")
    seen_mid_turn: list[list[ResumeStreamChunk]] = []
    lease_free_at_done: list[bool] = []

    async def stream(*_args, **_kwargs):
        yield _part(type="start")
        yield _delta("Visits rose ")
        await asyncio.sleep(0.1)
        seen_mid_turn.append(await _rows(thread.id))
        yield _delta("in March.")
        yield _part(type="finish")

    real_acreate = ResumeStreamChunk.objects.acreate

    async def acreate(**fields):
        if fields["done"]:
            row = await Thread.objects.aget(id=thread.id)
            lease_free_at_done.append(row.turn_lease_token is None)
        return await real_acreate(**fields)

    with (
        _agent_layer(stream=stream),
        patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.02),
        patch.object(ResumeStreamChunk.objects, "acreate", side_effect=acreate),
    ):
        resp = await _post_chat(client, ws, thread)
        body = b"".join([chunk async for chunk in resp.streaming_content])

    assert b"in March." in body
    # Readable while the turn ran, as another tab tails it.
    assert [row.text for row in seen_mid_turn[0]] == ["Visits rose "]
    rows = await _rows(thread.id)
    assert "".join(row.text for row in rows) == "Visits rose in March."
    assert [row.done for row in rows][-1] is True
    # A chat tailing it reloads on the done row, and finds the thread free.
    assert lease_free_at_done == [True]


async def test_a_turn_whose_writes_fail_still_answers():
    ws, thread, client = await _chat_member("live-turn-broken")

    async def stream(*_args, **_kwargs):
        yield _delta("Visits rose.")
        await asyncio.sleep(0.05)
        yield _part(type="finish")

    with (
        _agent_layer(stream=stream),
        patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01),
        patch.object(
            ResumeStreamChunk.objects, "acreate", AsyncMock(side_effect=RuntimeError("db"))
        ),
    ):
        resp = await _post_chat(client, ws, thread)
        body = b"".join([chunk async for chunk in resp.streaming_content])

    assert b"Visits rose." in body
    assert b'"finish"' in body
    refreshed = await Thread.objects.aget(id=thread.id)
    assert refreshed.turn_lease_token is None


async def test_writes_run_on_the_requests_own_thread_and_so_its_connection():
    thread = await _thread("live-thread")
    write_threads: set[int] = set()
    real_create = QuerySet.create

    def create(self, **fields):
        write_threads.add(threading.get_ident())
        return real_create(self, **fields)

    # As Django's ASGI handler does for each request.
    async with ThreadSensitiveContext():
        request_thread = await sync_to_async(threading.get_ident)()
        writer = LiveTurnWriter(thread.id)
        with (
            patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01),
            patch.object(QuerySet, "create", create),
        ):
            writer.observe(_delta("Visits rose."))
            await asyncio.sleep(0.05)
            writer.observe(_delta(" In March."))
            await writer.close()

    assert writer.rows_written >= 2
    assert write_threads == {request_thread}


async def test_a_hung_write_never_holds_up_the_end_of_the_turn():
    thread = await _thread("live-hung")
    writer = LiveTurnWriter(thread.id)

    async def hang(**_fields):
        await asyncio.Event().wait()

    with (
        patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01),
        patch.object(resume_stream, "LIVE_CLOSE_TIMEOUT_SECONDS", 0.2),
        patch.object(ResumeStreamChunk.objects, "acreate", side_effect=hang),
    ):
        writer.observe(_delta("Visits rose."))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await writer.close()

    assert time.monotonic() - started < 1


async def test_a_turn_left_mid_stream_still_ends_its_live_stream():
    ws, thread, client = await _chat_member("live-turn-left")

    async def stream(*_args, **_kwargs):
        yield _delta("Visits rose ")
        await asyncio.sleep(30)
        yield _delta("never sent")

    with (
        _agent_layer(stream=stream),
        patch.object(resume_stream, "LIVE_FLUSH_INTERVAL_SECONDS", 0.01),
    ):
        resp = await _post_chat(client, ws, thread)
        received = asyncio.Event()

        async def consume():
            async for _chunk in resp.streaming_content:
                received.set()

        # As Django does when the client goes: cancel the response mid-await.
        task = asyncio.create_task(consume())
        await received.wait()
        await asyncio.sleep(0.05)
        assert [row.text for row in await _rows(thread.id)] == ["Visits rose "]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    rows = await _rows(thread.id)
    assert [(row.text, row.done) for row in rows] == [("Visits rose ", False), ("", True)]
    refreshed = await Thread.objects.aget(id=thread.id)
    assert refreshed.turn_lease_token is None
