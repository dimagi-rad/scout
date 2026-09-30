"""A deleted Thread's checkpoints are kept (#265), and the checkpointer keys state on the
thread id alone. So a new Thread row must never be created for an id that already has
checkpoint state: whoever creates it would resume the old conversation.

These tests run against a real Postgres saver on the test database, because the guard
reads the checkpoint tables directly.
"""

import json
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.db import connection
from django.test import AsyncClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from psycopg.conninfo import make_conninfo

from apps.agents.tools.canvas_tool import create_canvas_read_tool
from apps.chat.checkpointer import thread_has_checkpoint
from apps.chat.checks import same_database
from apps.chat.models import Thread
from apps.common.db_urls import build_pg_url
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant
from tests.tenant_access import ausable_connection

User = get_user_model()

CHECKPOINT_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


class _State(TypedDict):
    messages: Annotated[list, add_messages]
    workspace_id: str
    user_id: str
    thread_id: str


def _echo(state: _State) -> dict:
    return {"messages": [AIMessage(content="ok")]}


def _stub_agent(checkpointer):
    graph = StateGraph(_State)
    graph.add_node("echo", _echo)
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)
    return graph.compile(checkpointer=checkpointer)


def _test_db_conninfo() -> str:
    db = connection.settings_dict
    params = {
        "dbname": db["NAME"],
        "host": db.get("HOST"),
        "port": db.get("PORT"),
        "user": db.get("USER"),
        "password": db.get("PASSWORD"),
    }
    return make_conninfo(**{key: str(value) for key, value in params.items() if value})


@pytest.fixture
def checkpoint_tables(transactional_db):
    """Create the saver's tables in the test database, and drop them afterwards so
    other tests keep seeing a database with no checkpoint state."""
    conninfo = _test_db_conninfo()
    with PostgresSaver.from_conn_string(conninfo) as saver:
        saver.setup()
    yield conninfo
    with connection.cursor() as cursor:
        for table in CHECKPOINT_TABLES:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")


@asynccontextmanager
async def _postgres_chat(conninfo):
    """Route the chat and thread views through a real Postgres saver and a stub graph."""
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:

        async def build(**kwargs):
            return _stub_agent(kwargs["checkpointer"])

        ensure = AsyncMock(return_value=saver)
        with (
            patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
            patch("apps.chat.views.ensure_checkpointer", ensure),
            patch("apps.chat.thread_views.ensure_checkpointer", ensure),
            patch("apps.chat.views.build_agent_graph", side_effect=build),
        ):
            yield saver


async def _member(email, ws, tenant):
    user = await User.objects.acreate_user(email=email, password="x")
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    client = AsyncClient()
    await client.alogin(email=email, password="x")
    return user, client


async def _workspace(slug):
    creator = await User.objects.acreate_user(email=f"creator-{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=creator)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name=f"{slug} Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    return ws, tenant


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
        _ = [chunk async for chunk in resp.streaming_content]
    return resp


async def _checkpointed_texts(saver, thread_id) -> list[str]:
    tup = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return []
    messages = tup.checkpoint["channel_values"].get("messages", [])
    return [m.content for m in messages if isinstance(m, HumanMessage)]


async def _deleted_thread(client, ws, slug) -> str:
    """A thread id whose conversation was checkpointed and whose Thread row was deleted."""
    thread_id = str(uuid.uuid4())
    resp = await _post(client, ws, thread_id, f"{slug}: old private question")
    assert resp.status_code == 200
    await Thread.objects.filter(id=thread_id).adelete()
    return thread_id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_post_does_not_resume_a_deleted_threads_checkpoints(checkpoint_tables):
    ws, tenant = await _workspace("retired-post")
    _, client = await _member("retired-post@b.c", ws, tenant)

    async with _postgres_chat(checkpoint_tables) as saver:
        thread_id = await _deleted_thread(client, ws, "post")
        resp = await _post(client, ws, thread_id, "new question")

        assert resp.status_code == 404
        assert resp.json() == {"error": "Thread not found"}
        assert not await Thread.objects.filter(id=thread_id).aexists()
        assert await _checkpointed_texts(saver, thread_id) == ["post: old private question"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_another_user_cannot_adopt_a_deleted_threads_id(checkpoint_tables):
    ws, tenant = await _workspace("retired-other")
    _, owner_client = await _member("retired-owner@b.c", ws, tenant)
    _, other_client = await _member("retired-other@b.c", ws, tenant)

    async with _postgres_chat(checkpoint_tables):
        thread_id = await _deleted_thread(owner_client, ws, "other")

        resp = await _post(other_client, ws, thread_id, "show me the earlier answer")

    assert resp.status_code == 404
    assert not await Thread.objects.filter(id=thread_id).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_patch_does_not_create_a_thread_over_a_deleted_threads_checkpoints(
    checkpoint_tables,
):
    ws, tenant = await _workspace("retired-patch")
    _, client = await _member("retired-patch@b.c", ws, tenant)

    async with _postgres_chat(checkpoint_tables):
        thread_id = await _deleted_thread(client, ws, "patch")

        patch_resp = await client.patch(
            f"/api/workspaces/{ws.id}/threads/{thread_id}/",
            data=json.dumps({"title": "renamed"}),
            content_type="application/json",
        )
        messages_resp = await client.get(f"/api/workspaces/{ws.id}/threads/{thread_id}/messages/")

    assert patch_resp.status_code == 404
    assert not await Thread.objects.filter(id=thread_id).aexists()
    # 404, not [] — the frontend drops the id and starts a fresh chat on a 404.
    assert messages_resp.status_code == 404
    assert b"old private question" not in messages_resp.content


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_owned_thread_resumes_its_conversation(checkpoint_tables):
    ws, tenant = await _workspace("resume")
    _, client = await _member("resume@b.c", ws, tenant)
    thread_id = str(uuid.uuid4())

    async with _postgres_chat(checkpoint_tables) as saver:
        assert (await _post(client, ws, thread_id, "first")).status_code == 200
        assert (await _post(client, ws, thread_id, "second")).status_code == 200
        patch_resp = await client.patch(
            f"/api/workspaces/{ws.id}/threads/{thread_id}/",
            data=json.dumps({"title": "renamed"}),
            content_type="application/json",
        )
        messages_resp = await client.get(f"/api/workspaces/{ws.id}/threads/{thread_id}/messages/")

        assert await _checkpointed_texts(saver, thread_id) == ["first", "second"]

    assert patch_resp.status_code == 200
    assert patch_resp.json()["title"] == "renamed"
    assert messages_resp.status_code == 200
    assert [m["role"] for m in messages_resp.json()] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_fresh_thread_id_can_be_named_before_its_first_message(checkpoint_tables):
    ws, tenant = await _workspace("fresh")
    user, client = await _member("fresh@b.c", ws, tenant)
    thread_id = str(uuid.uuid4())

    async with _postgres_chat(checkpoint_tables) as saver:
        messages_resp = await client.get(f"/api/workspaces/{ws.id}/threads/{thread_id}/messages/")
        patch_resp = await client.patch(
            f"/api/workspaces/{ws.id}/threads/{thread_id}/",
            data=json.dumps({"title": "named early"}),
            content_type="application/json",
        )
        post_resp = await _post(client, ws, thread_id, "hello")

        assert await _checkpointed_texts(saver, thread_id) == ["hello"]

    assert messages_resp.status_code == 200
    assert messages_resp.json() == []
    assert patch_resp.status_code == 200
    assert post_resp.status_code == 200
    thread = await Thread.objects.aget(id=thread_id)
    assert thread.user_id == user.pk
    assert thread.title == "named early"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_patch_on_another_users_thread_id_is_not_found():
    ws, tenant = await _workspace("patch-foreign")
    owner, _ = await _member("patch-owner@b.c", ws, tenant)
    _, other_client = await _member("patch-other@b.c", ws, tenant)
    thread = await Thread.objects.acreate(workspace=ws, user=owner, title="owner's")

    resp = await other_client.patch(
        f"/api/workspaces/{ws.id}/threads/{thread.id}/",
        data=json.dumps({"title": "hijacked"}),
        content_type="application/json",
    )

    assert resp.status_code == 404
    refreshed = await Thread.objects.aget(id=thread.id)
    assert refreshed.user_id == owner.pk
    assert refreshed.title == "owner's"


@pytest.mark.django_db
def test_thread_has_checkpoint_is_false_without_checkpoint_tables():
    # The test's transaction rolls this DDL back.
    with connection.cursor() as cursor:
        for table in CHECKPOINT_TABLES:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")

    assert thread_has_checkpoint(uuid.uuid4()) is False


def test_thread_has_checkpoint_is_false_for_an_id_without_state(checkpoint_tables):
    assert thread_has_checkpoint(uuid.uuid4()) is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_canvas_tool_does_not_recreate_a_deleted_thread(workspace, user):
    conversation_id = str(uuid.uuid4())
    tool = create_canvas_read_tool(workspace, user, conversation_id)

    result = await tool.ainvoke({"selector": "graph"})

    assert "no longer exists" in result
    assert not await Thread.objects.filter(id=conversation_id).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_canvas_tool_rejects_another_users_thread(workspace, user, read_user):
    thread = await Thread.objects.acreate(workspace=workspace, user=read_user)
    tool = create_canvas_read_tool(workspace, user, str(thread.id))

    result = await tool.ainvoke({"selector": "graph"})

    assert "another user" in result


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_canvas_tool_treats_a_non_uuid_conversation_as_gone(workspace, user):
    tool = create_canvas_read_tool(workspace, user, "")

    result = await tool.ainvoke({"selector": "graph"})

    assert "no longer exists" in result


@pytest.mark.parametrize(
    ("django_db", "conninfo", "expected"),
    [
        (
            {"NAME": "scout", "HOST": "db.internal", "PORT": 5432},
            "postgresql://scout:x@db.internal:5432/scout?sslmode=require",
            True,
        ),
        ({"NAME": "scout", "HOST": "localhost", "PORT": ""}, "postgresql://localhost/scout", True),
        (
            {"NAME": "other", "HOST": "db.internal", "PORT": 5432},
            "postgresql://scout:x@db.internal:5432/scout",
            False,
        ),
        (
            {"NAME": "scout", "HOST": "db.internal", "PORT": 5432},
            "postgresql://scout:x@replica.internal:5432/scout",
            False,
        ),
        (
            {"NAME": "scout", "HOST": "", "PORT": ""},
            "postgresql:///scout?host=/var/run/postgresql",
            True,
        ),
    ],
)
def test_same_database(django_db, conninfo, expected):
    assert same_database(django_db, conninfo) is expected


@pytest.fixture
def error_level_checks(settings):
    # Pinned so these tests keep exercising the error level if test settings change.
    settings.DEBUG = False
    return settings


@pytest.mark.parametrize(
    "url",
    ["postgresql://x@elsewhere:5432/not_scout", "pgsql://scout:pw@localhost/scout"],
)
def test_checkpointer_check_never_blocks_commands_under_test_settings(monkeypatch, url):
    monkeypatch.setattr("apps.chat.checks.get_database_url", lambda: url)

    blocking = [
        e
        for e in run_checks()
        if e.id.startswith("chat.") and e.is_serious() and not e.is_silenced()
    ]

    assert not blocking


def test_checkpointer_on_the_default_database_passes_the_system_check(
    error_level_checks, monkeypatch
):
    default = error_level_checks.DATABASES["default"]
    url = build_pg_url(
        host=str(default.get("HOST") or ""),
        port=default.get("PORT") or 5432,
        dbname=default["NAME"],
        user=str(default.get("USER") or ""),
        password=str(default.get("PASSWORD") or ""),
    )
    monkeypatch.setattr("apps.chat.checks.get_database_url", lambda: url)

    assert not {e.id for e in run_checks()} & {"chat.E001", "chat.E002"}


def test_unparseable_checkpointer_url_is_a_check_error_without_the_password(
    error_level_checks, monkeypatch
):
    monkeypatch.setattr(
        "apps.chat.checks.get_database_url", lambda: "pgsql://scout:hunter2@db.internal/scout"
    )

    errors = [e for e in run_checks() if e.id == "chat.E002"]

    assert len(errors) == 1
    assert "hunter2" not in str(errors[0])


def test_checkpointer_on_another_database_fails_the_system_check(error_level_checks, monkeypatch):
    monkeypatch.setattr(
        "apps.chat.checks.get_database_url", lambda: "postgresql://x@elsewhere:5432/not_scout"
    )

    assert "chat.E001" in {e.id for e in run_checks()}


def test_checkpointer_database_mismatch_only_warns_under_debug(error_level_checks, monkeypatch):
    error_level_checks.DEBUG = True
    monkeypatch.setattr(
        "apps.chat.checks.get_database_url", lambda: "postgresql://x@elsewhere:5432/not_scout"
    )

    ids = {e.id for e in run_checks()}

    assert "chat.W001" in ids
    assert "chat.E001" not in ids


def test_canvas_does_not_create_a_thread_over_a_deleted_threads_checkpoints(
    checkpoint_tables, client, workspace, user
):
    thread_id = str(uuid.uuid4())
    with PostgresSaver.from_conn_string(checkpoint_tables) as saver:
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        _stub_agent(saver).invoke(
            {
                "messages": [HumanMessage(content="old private question")],
                "workspace_id": str(workspace.id),
                "user_id": str(user.id),
                "thread_id": thread_id,
            },
            config,
        )
    assert thread_has_checkpoint(thread_id) is True

    client.force_login(user)
    response = client.get(f"/api/workspaces/{workspace.id}/threads/{thread_id}/canvas/")

    assert response.status_code == 404
    assert not Thread.objects.filter(id=thread_id).exists()
