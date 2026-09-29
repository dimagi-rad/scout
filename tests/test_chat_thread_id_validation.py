"""A chat turn's threadId is the LangGraph checkpointer key, which has no user or
workspace scoping of its own; the Thread row is the only authorization for it. These
tests pin that a threadId the Thread lookup can't match is rejected rather than
passed through to the checkpointer unauthorized."""

import json
import uuid
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from apps.chat.models import Thread
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from tests.tenant_access import ausable_connection

User = get_user_model()


class _State(TypedDict):
    messages: Annotated[list, add_messages]
    workspace_id: str
    user_id: str
    thread_id: str


def _echo(state: _State) -> dict:
    return {"messages": [AIMessage(content="ok")]}


def _stub_agent(checkpointer):
    """A real checkpointed graph with no LLM, so the view reads and writes checkpoints."""
    graph = StateGraph(_State)
    graph.add_node("echo", _echo)
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)
    return graph.compile(checkpointer=checkpointer)


async def _member(email, ws, tenant, *, raise_request_exception=True):
    user = await User.objects.acreate_user(email=email, password="x")
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    client = AsyncClient(raise_request_exception=raise_request_exception)
    await client.alogin(email=email, password="x")
    return user, client


@pytest.fixture
def checkpointer():
    saver = MemorySaver()

    async def build(**kwargs):
        return _stub_agent(kwargs["checkpointer"])

    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", new_callable=AsyncMock, return_value=saver),
        patch("apps.chat.views.build_agent_graph", side_effect=build),
    ):
        yield saver


async def _post(client, ws, thread_id, text):
    resp = await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [{"role": "user", "content": text}],
                "workspaceId": str(ws.id),
                "threadId": thread_id,
            }
        ),
        content_type="application/json",
    )
    if resp.streaming:
        # Drain the stream so the graph runs and writes its checkpoint.
        _ = [chunk async for chunk in resp.streaming_content]
    return resp


def _stored_thread_ids(saver) -> set[str]:
    # MemorySaver.storage is a defaultdict, so a read leaves an empty entry behind.
    return {
        thread_id for thread_id, namespaces in saver.storage.items() if any(namespaces.values())
    }


async def _checkpointed_texts(saver, thread_id) -> list[str]:
    tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return []
    messages = tup.checkpoint["channel_values"].get("messages", [])
    return [m.content for m in messages if isinstance(m, HumanMessage)]


async def _shared_workspace(slug):
    owner = await User.objects.acreate_user(email=f"creator-{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=owner)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name=f"{slug} Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    return ws, tenant


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("thread_id", ["abc", " ", "recipe-run-1", "1234"])
async def test_non_uuid_thread_id_is_rejected(checkpointer, thread_id):
    ws, tenant = await _shared_workspace(f"nonuuid-{uuid.uuid4().hex[:8]}")
    _, client = await _member(f"u-{uuid.uuid4().hex[:8]}@b.c", ws, tenant)

    resp = await _post(client, ws, thread_id, "hello")

    assert resp.status_code == 400
    assert resp.json() == {"error": "threadId must be a UUID"}
    assert _stored_thread_ids(checkpointer) == set()
    assert not await Thread.objects.aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_two_users_sharing_a_non_uuid_thread_id_never_share_checkpoint_state(
    checkpointer,
):
    ws, tenant = await _shared_workspace("shared-id")
    _, client_a = await _member("user-a@b.c", ws, tenant)
    _, client_b = await _member("user-b@b.c", ws, tenant)

    resp_a = await _post(client_a, ws, "same-id", "user A's private question")
    resp_b = await _post(client_b, ws, "same-id", "user B's question")

    assert resp_a.status_code == 400
    assert resp_b.status_code == 400
    assert "user A's private question" not in await _checkpointed_texts(checkpointer, "same-id")
    assert _stored_thread_ids(checkpointer) == set()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_other_users_uuid_thread_is_rejected_before_its_checkpoint_is_read(checkpointer):
    ws, tenant = await _shared_workspace("foreign-uuid")
    user_a, client_a = await _member("owner-a@b.c", ws, tenant)
    _, client_b = await _member("intruder-b@b.c", ws, tenant)
    thread_id = str(uuid.uuid4())

    resp_a = await _post(client_a, ws, thread_id, "user A's private question")
    assert resp_a.status_code == 200
    assert (await Thread.objects.aget(id=thread_id)).user_id == user_a.pk

    # A non-canonical spelling of the same UUID must resolve to the same row, not
    # to a fresh checkpointer key that skips the ownership check.
    for spelling in (thread_id, thread_id.upper(), thread_id.replace("-", "")):
        resp_b = await _post(client_b, ws, spelling, "user B's question")
        assert resp_b.status_code == 404

    assert await _checkpointed_texts(checkpointer, thread_id) == ["user A's private question"]
    assert _stored_thread_ids(checkpointer) == {thread_id}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_created_by_another_user_after_lookup_is_rejected(checkpointer):
    """The row can appear between the ownership lookup and the upsert; the upsert
    must not adopt it and the turn must not reach the checkpointer."""
    ws, tenant = await _shared_workspace("race")
    user_a, _ = await _member("race-a@b.c", ws, tenant)
    _, client_b = await _member("race-b@b.c", ws, tenant)
    thread = await Thread.objects.acreate(workspace=ws, user=user_a, title="A")

    real_filter = Thread.objects.filter

    def _lookup_misses(*args, **kwargs):
        qs = real_filter(*args, **kwargs)
        if kwargs == {"id": str(thread.id)}:
            qs.afirst = AsyncMock(return_value=None)
        return qs

    with patch.object(Thread.objects, "filter", side_effect=_lookup_misses):
        resp = await _post(client_b, ws, str(thread.id), "user B's question")

    assert resp.status_code == 404
    assert _stored_thread_ids(checkpointer) == set()
    refreshed = await Thread.objects.aget(id=thread.id)
    assert refreshed.user_id == user_a.pk
    assert refreshed.updated_at == thread.updated_at


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_canonical_thread_id_is_the_checkpointer_key(checkpointer):
    ws, tenant = await _shared_workspace("canonical")
    user, client = await _member("canon@b.c", ws, tenant)
    thread_id = str(uuid.uuid4())

    resp = await _post(client, ws, thread_id.upper(), "hi")

    assert resp.status_code == 200
    assert _stored_thread_ids(checkpointer) == {thread_id}
    assert (await Thread.objects.aget(id=thread_id)).user_id == user.pk


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_failed_thread_upsert_never_reaches_the_agent(checkpointer):
    ws, tenant = await _shared_workspace("upsert-fail")
    # A propagated view error becomes a 500 response, as in production.
    _, client = await _member("upsert-fail@b.c", ws, tenant, raise_request_exception=False)

    with (
        patch(
            "apps.chat.views._upsert_thread",
            side_effect=OperationalError("server closed the connection unexpectedly"),
        ),
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock) as mcp_tools,
    ):
        resp = await _post(client, ws, str(uuid.uuid4()), "hi")

    assert resp.status_code == 500
    mcp_tools.assert_not_called()
    assert _stored_thread_ids(checkpointer) == set()
