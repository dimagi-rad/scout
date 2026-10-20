"""Attachments end to end: agent_node, the held path, and binding races (#913)."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest
from asgiref.sync import sync_to_async
from django.test import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage

from apps.agents.graph.base import build_agent_graph
from apps.chat.attachments import (
    SCOUT_ATTACHMENTS_KWARG,
    abind_to_thread,
    amark_sent,
    attachment_url,
    aunbind,
)
from apps.chat.models import AttachmentBlob, ChatAttachment, PendingRequest, Thread
from apps.chat.views import _TurnStreamingResponse
from tests import image_fixtures as images

PNG = images.png()


async def _attachment(user, workspace, thread=None, content=PNG, media_type="image/png"):
    blob = await AttachmentBlob.objects.acreate(
        sha256=uuid.uuid4().hex + uuid.uuid4().hex, size=len(content), content=content
    )
    return await ChatAttachment.objects.acreate(
        blob=blob,
        user=user,
        workspace=workspace,
        thread=thread,
        filename="shot.png",
        media_type=media_type,
    )


def _entry(attachment):
    return {
        "id": str(attachment.id),
        "filename": attachment.filename,
        "media_type": attachment.media_type,
        "size": len(PNG),
    }


def _image_rejection(
    message="messages.0.content.0.image.source.base64: does not appear to be a valid png image",
):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(
        message, response=httpx.Response(400, request=request), body=None
    )


async def _run_agent(thread, message, *, side_effect):
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=side_effect)
    llm = MagicMock()
    llm.bind_tools.return_value = bound
    with (
        patch("apps.agents.graph.base.ChatAnthropic", return_value=llm),
        patch("apps.agents.graph.base._build_tools", return_value=[]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            new=AsyncMock(return_value=("prompt", "")),
        ),
    ):
        graph = await build_agent_graph(
            SimpleNamespace(id="ws-1"),
            MagicMock(is_authenticated=False),
            conversation_id=str(thread.id),
        )
        result = await graph.ainvoke({"messages": [message]})
    return bound.ainvoke.await_args_list, result["messages"]


def _blocks(call):
    messages = call.args[0]
    return messages[-1].content


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_agent_node_sends_attachments_as_content_blocks(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace)
    image = await _attachment(user, workspace, thread)
    message = HumanMessage(
        content="what is this?", additional_kwargs={SCOUT_ATTACHMENTS_KWARG: [_entry(image)]}
    )

    calls, messages = await _run_agent(thread, message, side_effect=[AIMessage(content="a chart")])

    [call] = calls
    image_block, text_block = _blocks(call)
    assert image_block["type"] == "image"
    assert image_block["source"]["media_type"] == "image/png"
    assert text_block == {"type": "text", "text": "what is this?"}
    # The checkpointed message keeps its text content; only the model's copy expands.
    assert messages[0].content == "what is this?"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_agent_node_retries_without_an_image_the_api_refused(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace)
    image = await _attachment(user, workspace, thread)
    message = HumanMessage(
        content="what is this?", additional_kwargs={SCOUT_ATTACHMENTS_KWARG: [_entry(image)]}
    )

    calls, messages = await _run_agent(
        thread, message, side_effect=[_image_rejection(), AIMessage(content="I can't see it")]
    )

    first, second = calls
    assert _blocks(first)[0]["type"] == "image"
    note, text = _blocks(second)
    assert note == {
        "type": "text",
        "text": f'[Image "shot.png" (id {image.id}) could not be processed.]',
    }
    assert text["text"] == "what is this?"
    assert messages[-1].content == "I can't see it"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_agent_node_does_not_retry_other_bad_requests(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace)
    image = await _attachment(user, workspace, thread)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    other = anthropic.BadRequestError(
        "prompt is too long", response=httpx.Response(400, request=request), body=None
    )
    message = HumanMessage(
        content="hi", additional_kwargs={SCOUT_ATTACHMENTS_KWARG: [_entry(image)]}
    )

    with pytest.raises(anthropic.BadRequestError):
        await _run_agent(thread, message, side_effect=[other, AIMessage(content="x")])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_refused_request_never_unbinds_what_an_accepted_one_carries(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace)
    shared = await _attachment(user, workspace)
    # Request 1 binds it, request 2 finds it bound and is accepted, request 1 is refused.
    first = await abind_to_thread([str(shared.id)], user=user, workspace=workspace, thread=thread)
    second = await abind_to_thread([str(shared.id)], user=user, workspace=workspace, thread=thread)
    assert (first, second) == ([str(shared.id)], [])
    await amark_sent([str(shared.id)], user=user, thread=thread)
    await aunbind(first, thread=thread)

    await shared.arefresh_from_db()
    assert shared.thread_id == thread.id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_turn_closed_before_it_started_unbinds_its_attachments(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace)
    attachment = await _attachment(user, workspace, thread)
    lease = MagicMock(thread_id=str(thread.id))
    response = _TurnStreamingResponse(lease, bound={"ids": [str(attachment.id)], "thread": thread})

    await sync_to_async(response.close)()

    lease.release_sync.assert_called_once()
    await attachment.arefresh_from_db()
    assert attachment.thread_id is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_held_message_keeps_its_attachments_bound_and_sent(user, workspace):
    attachment = await _attachment(user, workspace)
    thread_id = str(uuid.uuid4())
    client = AsyncClient()
    await client.alogin(email=user.email, password="testpass123")
    with (
        patch("apps.chat.views.aworkspace_schema_status", AsyncMock(return_value="loading")),
        patch("apps.chat.views.athread_awaits_load", AsyncMock(return_value=False)),
        patch("apps.chat.views.aworkspace_own_build_pending", AsyncMock(return_value=True)),
        patch("apps.chat.views.astart_chat_load", AsyncMock(return_value=object())),
    ):
        resp = await client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [
                        {
                            "id": "m1",
                            "role": "user",
                            "parts": [
                                {
                                    "type": "file",
                                    "mediaType": "image/png",
                                    "url": attachment_url(attachment.id),
                                }
                            ],
                        }
                    ],
                    "workspaceId": str(workspace.id),
                    "threadId": thread_id,
                }
            ),
            content_type="application/json",
        )
        body = b"".join([chunk async for chunk in resp.streaming_content])

    assert b"data-pending-request" in body
    pending = await PendingRequest.objects.aget(thread_id=thread_id)
    assert pending.parts[0]["attachments"][0]["id"] == str(attachment.id)
    await attachment.arefresh_from_db()
    assert str(attachment.thread_id) == thread_id
    assert attachment.sent_at is not None


def _note(attachment):
    return {
        "type": "text",
        "text": f'[Image "shot.png" (id {attachment.id}) could not be processed.]',
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_only_the_image_the_api_named_is_stubbed_and_remembered(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace)
    good = await _attachment(user, workspace, thread, content=images.png(5, 5))
    bad = await _attachment(user, workspace, thread, content=images.png(6, 6))
    message = HumanMessage(
        content="compare",
        additional_kwargs={SCOUT_ATTACHMENTS_KWARG: [_entry(good), _entry(bad)]},
    )
    rejection = _image_rejection(
        "messages.0.content.1.image.source.base64: does not appear to be a valid png image"
    )

    calls, _messages = await _run_agent(
        thread, message, side_effect=[rejection, AIMessage(content="one of them")]
    )

    _first, second = calls
    kept, stubbed, _text = _blocks(second)
    assert kept["type"] == "image"
    assert stubbed == _note(bad)
    await good.arefresh_from_db()
    await bad.arefresh_from_db()
    assert good.rejected_at is None
    assert bad.rejected_at is not None

    # A later turn builds a new loader, and stubs it up front without a failing call.
    calls, _messages = await _run_agent(thread, message, side_effect=[AIMessage(content="ok")])
    [call] = calls
    kept, stubbed, _text = _blocks(call)
    assert kept["type"] == "image"
    assert stubbed == _note(bad)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_unmappable_image_rejection_stubs_every_image(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace)
    first = await _attachment(user, workspace, thread, content=images.png(5, 5))
    second = await _attachment(user, workspace, thread, content=images.png(6, 6))
    message = HumanMessage(
        content="compare",
        additional_kwargs={SCOUT_ATTACHMENTS_KWARG: [_entry(first), _entry(second)]},
    )

    calls, _messages = await _run_agent(
        thread,
        message,
        side_effect=[_image_rejection("Could not process image"), AIMessage(content="ok")],
    )

    stub_a, stub_b, _text = _blocks(calls[1])
    assert (stub_a, stub_b) == (_note(first), _note(second))
