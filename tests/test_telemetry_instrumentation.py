"""Chat turns, tool calls, loads, recipes and feature use each leave a telemetry event."""

import asyncio
import contextlib
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from django.test import AsyncClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from apps.chat.models import Thread
from apps.chat.stream import langgraph_to_ui_stream
from apps.telemetry.agent_runs import AgentRunTelemetry, _tool_result, _usage
from apps.telemetry.models import EventKind, Outcome, TelemetryEvent
from apps.users.models import Tenant, TenantMembership, User
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from tests.agent_doubles import FakeAgent
from tests.tenant_access import ausable_connection


async def _events(kind):
    return [e async for e in TelemetryEvent.objects.filter(kind=kind).order_by("id")]


@tool
def lookup_rows(question: str) -> str:
    """Look up rows."""
    return json.dumps(
        {"success": False, "error": {"code": "QUERY_TIMEOUT", "message": "slow"}, "timing_ms": 7}
    )


def _tool_then_answer_graph():
    call = AIMessage(
        content="",
        tool_calls=[{"name": "lookup_rows", "args": {"question": "q"}, "id": "toolu_1"}],
    )
    model = FakeMessagesListChatModel(responses=[call, AIMessage(content="Done.")])

    async def agent(state: MessagesState) -> dict:
        return {"messages": [await model.ainvoke(state["messages"])]}

    def route(state: MessagesState) -> str:
        return "tools" if state["messages"][-1].tool_calls else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([lookup_rows]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route)
    graph.add_edge("tools", "agent")
    return graph.compile()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_streamed_turn_records_itself_and_its_tool_calls():
    telemetry = AgentRunTelemetry(
        EventKind.CHAT_TURN, user_id=7, workspace_id=uuid.uuid4(), thread_id=str(uuid.uuid4())
    )
    config = {"configurable": {"thread_id": "t"}, "callbacks": [telemetry]}

    stream = langgraph_to_ui_stream(
        _tool_then_answer_graph(), {"messages": [("user", "hi")]}, config
    )
    chunks = [chunk async for chunk in telemetry.wrap_stream(stream)]

    assert chunks[-1].startswith('data: {"type": "finish"')
    [turn] = await _events(EventKind.CHAT_TURN)
    assert turn.outcome == Outcome.COMPLETED
    assert turn.user_id == 7
    assert turn.attrs["tool_calls"] == 1
    assert turn.attrs["tool_errors"] == 1
    assert turn.attrs["llm_calls"] == 2
    [call] = await _events(EventKind.TOOL_CALL)
    assert (call.name, call.outcome) == ("lookup_rows", Outcome.ERROR)
    assert call.attrs["error_code"] == "QUERY_TIMEOUT"
    assert call.attrs["server_ms"] == 7
    assert call.attrs["run_kind"] == EventKind.CHAT_TURN


async def _sse_stream(*chunks, then=None):
    for chunk in chunks:
        yield chunk
    if then is not None:
        await then()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_error_chunk_marks_the_turn_failed():
    telemetry = AgentRunTelemetry(EventKind.CHAT_TURN)
    stream = _sse_stream(
        'data: {"type": "error", "errorText": "x"}\n\n', 'data: {"type": "finish"}\n\n'
    )

    _ = [chunk async for chunk in telemetry.wrap_stream(stream)]

    [turn] = await _events(EventKind.CHAT_TURN)
    assert turn.outcome == Outcome.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_retryable_status_marks_the_turn_failed():
    telemetry = AgentRunTelemetry(EventKind.CHAT_TURN)
    status = {"type": "data-chat-status", "data": {"kind": "retryable-error"}}
    stream = _sse_stream(f"data: {json.dumps(status)}\n\n", 'data: {"type": "finish"}\n\n')

    _ = [chunk async for chunk in telemetry.wrap_stream(stream)]

    [turn] = await _events(EventKind.CHAT_TURN)
    assert turn.outcome == Outcome.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_client_disconnect_marks_the_turn_stopped():
    telemetry = AgentRunTelemetry(EventKind.CHAT_TURN)
    wrapped = telemetry.wrap_stream(
        _sse_stream(
            'data: {"type": "text-delta", "id": "t", "delta": "a"}\n\n', then=asyncio.Event().wait
        )
    )

    await anext(wrapped)
    await wrapped.aclose()

    [turn] = await _events(EventKind.CHAT_TURN)
    assert turn.outcome == Outcome.STOPPED
    assert "ttft_ms" in turn.attrs


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_stream_that_raises_is_recorded_failed_and_still_raises():
    async def boom():
        raise RuntimeError("boom")

    telemetry = AgentRunTelemetry(EventKind.CHAT_TURN)

    with pytest.raises(RuntimeError):
        _ = [chunk async for chunk in telemetry.wrap_stream(_sse_stream(then=boom))]

    [turn] = await _events(EventKind.CHAT_TURN)
    assert turn.outcome == Outcome.FAILED


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_failed_telemetry_write_never_reaches_the_turn():
    telemetry = AgentRunTelemetry(EventKind.CHAT_TURN)
    with patch.object(TelemetryEvent.objects, "bulk_create", side_effect=RuntimeError("down")):
        chunks = [
            chunk
            async for chunk in telemetry.wrap_stream(_sse_stream('data: {"type": "finish"}\n\n'))
        ]

    assert chunks == ['data: {"type": "finish"}\n\n']


def test_token_usage_is_summed_across_generations():
    message = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 80, "cache_creation": 5},
        },
        response_metadata={"model_name": "claude-test-1"},
    )
    result = LLMResult(generations=[[ChatGeneration(message=message)]])

    counts, model = _usage(result)

    assert counts == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 80,
        "cache_creation_tokens": 5,
    }
    assert model == "claude-test-1"


def test_on_llm_end_accumulates_usage_into_the_run_event():
    telemetry = AgentRunTelemetry(EventKind.CHAT_TURN)
    message = AIMessage(
        content="x", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    )
    for _ in range(2):
        telemetry.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=uuid.uuid4()
        )

    [turn] = telemetry.events()
    assert turn.attrs["input_tokens"] == 6
    assert turn.attrs["output_tokens"] == 4
    assert turn.attrs["llm_calls"] == 2


def test_tool_status_reads_the_envelope_and_the_message_status():
    ok = json.dumps({"success": True, "data": {"rows": [1]}, "timing_ms": 12})
    assert _tool_result(ToolMessage(content=ok, tool_call_id="a")) == (
        Outcome.OK,
        {"server_ms": 12},
    )
    errored = ToolMessage(content="Error: bad args", tool_call_id="a", status="error")
    assert _tool_result(errored) == (Outcome.ERROR, {})


def test_a_raised_tool_error_is_counted():
    telemetry = AgentRunTelemetry(EventKind.CHAT_TURN)
    run_id = uuid.uuid4()
    telemetry.on_tool_start({"name": "execute_sql"}, "", run_id=run_id)
    telemetry.on_tool_error(TimeoutError(), run_id=run_id)

    turn, call = telemetry.events()
    assert turn.attrs["tool_errors"] == 1
    assert (call.name, call.outcome, call.attrs["error_type"]) == (
        "execute_sql",
        Outcome.ERROR,
        "TimeoutError",
    )


async def _chat_member(slug: str):
    user = await User.objects.acreate_user(email=f"{slug}@b.c", password="x")
    ws = await Workspace.objects.acreate(name=f"W-{slug}", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id=f"t-{slug}", provider="commcare", canonical_name="Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.READ)
    await TenantMembership.objects.acreate(
        user=user, tenant=tenant, connection=await ausable_connection(user, tenant.provider)
    )
    thread = await Thread.objects.acreate(workspace=ws, user=user)
    client = AsyncClient()
    await client.alogin(email=f"{slug}@b.c", password="x")
    return user, ws, thread, client


@contextlib.contextmanager
def _agent(agent):
    with (
        patch("apps.chat.views.get_mcp_tools", new_callable=AsyncMock, return_value=[]),
        patch("apps.chat.views.ensure_checkpointer", new_callable=AsyncMock),
        patch("apps.chat.views.build_agent_graph", AsyncMock(return_value=agent)),
        patch(
            "apps.chat.views.repair_dangling_tool_calls", new_callable=AsyncMock, return_value=[]
        ),
        patch("apps.chat.views.aschedule_thread_title", new_callable=AsyncMock),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_chat_turn_through_the_view_records_one_turn():
    user, ws, thread, client = await _chat_member("telemetry-turn")
    await TelemetryEvent.objects.all().adelete()

    with _agent(FakeAgent()):
        resp = await client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "how many visits?"}],
                    "workspaceId": str(ws.id),
                    "threadId": str(thread.id),
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        _ = [chunk async for chunk in resp.streaming_content]

    [turn] = await _events(EventKind.CHAT_TURN)
    assert turn.outcome == Outcome.COMPLETED
    assert turn.user_id == user.id
    assert turn.workspace_id == ws.id
    assert turn.attrs["thread_id"] == str(thread.id)
    assert turn.attrs["held_request"] is False
    assert "ttft_ms" in turn.attrs
    assert "how many" not in json.dumps(turn.attrs)
