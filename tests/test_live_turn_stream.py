"""A chat turn's text, written for chats that did not start it (#856)."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

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
    for token in tokens:
        writer.observe(_delta(token))
        await asyncio.sleep(0.02)
    await asyncio.sleep(0)
    streamed = await _rows(thread.id)

    elapsed_intervals = 150 * 0.02 / resume_stream.LIVE_FLUSH_INTERVAL_SECONDS
    assert 1 <= len(streamed) <= elapsed_intervals + 1
    assert all(not row.done for row in streamed)
    assert {row.run for row in streamed} == {writer.run}

    await writer.close()

    rows = await _rows(thread.id)
    # The answer is in the checkpoint now: only the done row is kept, with what the
    # last interval had not written yet.
    assert [row.done for row in rows] == [True]
    assert "".join(row.text for row in streamed) + rows[0].text == "".join(tokens)
    assert writer.rows_written == len(streamed) + 1


async def test_text_after_a_tool_is_kept_apart_from_text_before_it():
    thread = await _thread("live-calls")
    writer = LiveTurnWriter(thread.id)

    writer.observe(_delta("Let me check."))
    writer.observe(_part(type="text-end", id="text-0"))
    writer.observe(_part(type="tool-input-available", toolCallId="t1", toolName="q", input={}))
    writer.observe(_part(type="tool-output-available", toolCallId="t1", output="rows"))
    writer.observe(_delta("Here it is."))
    await writer.close()

    rows = await _rows(thread.id)
    assert rows[-1].text == f"Let me check.{resume_stream.CALL_SEPARATOR}Here it is."


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
    assert [(row.text, row.done) for row in rows] == [("in March.", True)]
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
