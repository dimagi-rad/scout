"""Real (unmocked) chat<->MCP contract tests.

These tests stand up the REAL ``mcp_server.server.mcp`` FastMCP instance over the
official MCP SDK's in-memory transport (``create_connected_server_and_client_session``)
and load its tools with the SAME ``langchain_mcp_adapters`` loader the agent graph
uses (:func:`apps.agents.mcp_client.get_mcp_tools`). NOTHING on the wire is mocked:
the LangChain tool, the MCP protocol round-trip, the FastMCP arg parsing, and the
tool handler all run for real. Only the managed-DB-facing data path is avoided where
a tool can be exercised without materialized data.

This is the guardrail described in arch-review issue #234 (finding 10#4). The
existing suite mocks exactly this seam:

- ``test_mcp_client`` patches ``MultiServerMCPClient``
- ``test_mcp_chat_integration`` mocks ``get_mcp_tools`` with hand-written fakes
- ``test_agent_graph`` asserts ``MCP_TOOL_NAMES`` against itself

so prompt-vs-schema drift (02#6), arg-validation-by-omission (07#0), and the
chat->MCP wire as a whole (10#4) go undetected. Each contract below asserts what
SHOULD hold. Contracts currently broken by another tracked issue carry a real
assertion plus ``@pytest.mark.xfail(strict=False)`` so CI stays green and an xpass
signals the moment the blocking issue is fixed.

DB note: tools are exercised against the platform Django test DB only. ``list_pipelines``
needs no DB; ``get_schema_status`` returns ``not_provisioned`` for a workspace whose
schema was never materialized, so no managed schema/role creation is required here and
the tests are hermetic.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from apps.agents.graph.base import (
    AGENT_EXCLUDED_MCP_TOOLS,
    MCP_TOOL_NAMES,
    _fetch_schema_context,
    _make_injecting_tool_node,
)
from apps.agents.graph.state import AgentState
from apps.users.models import Tenant
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from mcp_server.server import mcp as scout_mcp

pytestmark = pytest.mark.asyncio(loop_scope="function")


# Tools the server exposes that do NOT take a server-injected context id. Kept
# explicit so adding a context-free tool is a deliberate edit, not silent drift.
# ``get_materialization_status``/``cancel_materialization`` were moved OUT of this
# set (arch #253, 01#6): they now take an injected ``workspace_id`` that scopes
# the LLM-supplied ``run_id`` to the calling workspace, so they belong in
# ``MCP_TOOL_NAMES`` instead.
CONTEXT_FREE_TOOLS = frozenset(
    {
        "list_pipelines",
    }
)

# Server-injected params: added by the agent graph's injecting tool node, never
# supplied by the LLM. Every tool in MCP_TOOL_NAMES must advertise ``workspace_id``;
# only run_materialization needs the full set.
INJECTED_PARAMS = {"workspace_id", "user_id", "thread_id", "tool_call_id"}


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def mcp_wire() -> AsyncIterator[tuple[ClientSession, dict[str, Any]]]:
    """Real in-process MCP wire: (session, {tool_name: LangChain tool}).

    Runs ``mcp_server.server.mcp`` in a task group over anyio memory streams — the
    same FastMCP instance served in production over streamable-http, just with the
    transport swapped for an in-memory pair — and loads its tools with the exact
    ``langchain_mcp_adapters.load_mcp_tools`` loader ``get_mcp_tools`` uses. No tool,
    handler, protocol step, or adapter is mocked.

    Implemented as a plain ``@asynccontextmanager`` (not a pytest fixture) and used
    via ``async with`` inside each test so the server task group is entered and
    exited in the SAME task — pytest-asyncio finalizers can run a yield-fixture's
    teardown in a different task, which trips anyio's cancel-scope guard.
    """
    async with create_connected_server_and_client_session(scout_mcp) as session:
        tools = await load_mcp_tools(session)
        yield session, {t.name: t for t in tools}


def parse_tool_result(result: Any) -> dict:
    """Parse a LangChain MCP tool result into the JSON envelope dict.

    The real wire returns a list of content blocks (``[{"type": "text", "text": ...}]``);
    a few adapter versions return a bare string. This mirrors how the agent/frontend
    must decode tool output, so testing it is part of the contract.
    """
    if isinstance(result, str):
        return json.loads(result)
    if isinstance(result, list):
        first = result[0]
        text = first["text"] if isinstance(first, dict) else getattr(first, "text", first)
        return json.loads(text)
    raise AssertionError(f"Unexpected tool result shape: {type(result)}: {result!r}")


# --------------------------------------------------------------------------- #
# Contract 1: tool-set drift detector
# --------------------------------------------------------------------------- #


async def test_advertised_tool_set_matches_graph_expectation():
    """The tools advertised over the real wire == the set the graph expects.

    ``MCP_TOOL_NAMES`` is the contract the agent graph relies on to inject context
    ids; today its only test asserts it against itself. This pins it against the
    REAL server's advertised tools, so a rename/add/remove in ``server.py`` that
    desyncs the graph fails here (the recipe-class drift, 10#4 / 02#6).
    """
    async with mcp_wire() as (_session, tools):
        advertised = set(tools)
    # ``AGENT_EXCLUDED_MCP_TOOLS`` (e.g. the destructive ``teardown_schema``) are
    # advertised by the server for operator/HTTP callers but deliberately NOT bound
    # to the agent (arch #237 / finding 00#2). They are a third accounted-for bucket
    # alongside graph-injected (``MCP_TOOL_NAMES``) and ``CONTEXT_FREE_TOOLS`` tools.
    # The drift-detection intent is preserved: a NEW server tool that is neither
    # expected, context-free, nor explicitly excluded still trips the ``extra`` check.
    expected = set(MCP_TOOL_NAMES) | CONTEXT_FREE_TOOLS | AGENT_EXCLUDED_MCP_TOOLS

    missing = expected - advertised
    extra = advertised - expected
    assert not missing, f"Tools the graph expects but the server no longer exposes: {missing}"
    assert not extra, (
        f"Tools the server exposes that the graph does not account for: {extra}. "
        "Add to MCP_TOOL_NAMES (if context-injected) or CONTEXT_FREE_TOOLS."
    )


async def test_every_injected_tool_advertises_workspace_id():
    """Every context-injected tool advertises ``workspace_id`` on the real wire.

    The injecting tool node sets ``workspace_id`` on every call whose name is in
    ``MCP_TOOL_NAMES``. If the server-side param were renamed/removed, injection
    would silently no-op (07#0). Asserted against the raw ``inputSchema`` the
    server actually advertises, not a hand-written fake.
    """
    async with mcp_wire() as (session, _tools):
        resp = await session.list_tools()
        schema_by_name = {t.name: (t.inputSchema or {}) for t in resp.tools}

    for name in MCP_TOOL_NAMES:
        props = set(schema_by_name[name].get("properties", {}))
        assert "workspace_id" in props, (
            f"Tool {name!r} (in MCP_TOOL_NAMES) does not advertise 'workspace_id' on the "
            f"wire; context injection would silently no-op. Advertised: {sorted(props)}"
        )


# --------------------------------------------------------------------------- #
# Contract 2: per-tool parameter-schema contract (drift detector for 02#6/07#0)
# --------------------------------------------------------------------------- #


async def test_run_materialization_advertises_all_injected_params():
    """run_materialization advertises every server-injected param, and no pipeline=.

    Two sub-contracts on the real wire:
      * all four injected params (workspace_id/user_id/thread_id/tool_call_id) are
        present, so the injecting node's writes are accepted (07#0); and
      * there is NO ``pipeline`` param — the routing moved into materialize_workspace,
        so any prompt/tool that still passes ``pipeline=`` is drifting (02#6).
    """
    async with mcp_wire() as (session, _tools):
        resp = await session.list_tools()
    schema = next(t.inputSchema for t in resp.tools if t.name == "run_materialization")
    props = set((schema or {}).get("properties", {}))

    assert props >= INJECTED_PARAMS, (
        f"run_materialization missing injected params {INJECTED_PARAMS - props}; "
        "the injecting tool node would write args the tool drops or rejects."
    )
    assert "pipeline" not in props, (
        "run_materialization advertises a 'pipeline' param again — if so, the prompt "
        "drift assertion below should be flipped back to passing."
    )


# --------------------------------------------------------------------------- #
# Contract 3: prompt-vs-tool-schema drift — the live `pipeline=` instruction
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
@pytest.mark.xfail(
    reason="blocked by #238: single-tenant schema-context prompt still instructs "
    "run_materialization(pipeline=...) but the tool has no pipeline param",
    strict=False,
)
async def test_prompt_does_not_reference_params_absent_from_tool_schema(db):
    """The agent prompt must not instruct a tool param the real wire does not expose.

    Builds the REAL single-tenant ``## Data Availability`` prompt section and checks
    it against the real ``run_materialization`` schema. The prompt currently tells
    the agent to call ``run_materialization`` with ``pipeline="..."`` while the wire
    advertises no such param — the recurring prompt/contract drift class (02#6).
    Asserts the CORRECT contract; xfail-tracked under #238.
    """
    async with mcp_wire() as (session, _tools):
        resp = await session.list_tools()
    schema = next(t.inputSchema for t in resp.tools if t.name == "run_materialization")
    tool_params = set((schema or {}).get("properties", {}))

    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="contract-prompt-domain", canonical_name="Contract Prompt"
    )
    # No TenantSchema => the "no data loaded" branch that emits the pipeline= text.
    prompt_section = await _fetch_schema_context(tenant, None)

    if "pipeline=" in prompt_section or 'pipeline="' in prompt_section:
        assert "pipeline" in tool_params, (
            "Prompt instructs run_materialization with a `pipeline=` argument, but the "
            "real tool schema exposes no such param — prompt/contract drift (02#6, #238). "
            f"Tool params: {sorted(tool_params)}"
        )


# --------------------------------------------------------------------------- #
# Contract 4: real round-trip result shape
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
async def test_list_pipelines_round_trip_shape():
    """list_pipelines round-trips and returns the documented success envelope.

    A full client->server->handler->client round trip over the real protocol,
    decoded the way the agent/frontend must. Pins the envelope shape
    (``success`` + ``data.pipelines[*]`` with name/provider) the consumers rely on.
    """
    async with mcp_wire() as (_session, tools):
        raw = await tools["list_pipelines"].ainvoke({})
    env = parse_tool_result(raw)

    assert env["success"] is True
    assert "data" in env and "pipelines" in env["data"]
    pipelines = env["data"]["pipelines"]
    assert isinstance(pipelines, list) and pipelines, "expected at least one registered pipeline"
    for p in pipelines:
        assert "name" in p and "provider" in p


@pytest.mark.django_db(transaction=True)
async def test_get_schema_status_round_trip_not_provisioned(db):
    """get_schema_status round-trips for a never-materialized workspace.

    Hits the platform DB for real (no mock at the ORM boundary) and returns the
    documented ``exists/state/last_materialized_at/tables`` shape with
    ``state == "not_provisioned"``. This is the card the frontend renders; pinning
    the shape guards the get_metadata/status "0 tables" class (10#4, tracked
    elsewhere under #246/#251).
    """
    user = await _make_user("schema-status@example.com")
    ws = await Workspace.objects.acreate(name="No Data WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)

    async with mcp_wire() as (_session, tools):
        raw = await tools["get_schema_status"].ainvoke({"workspace_id": str(ws.id)})
    env = parse_tool_result(raw)

    assert env["success"] is True
    data = env["data"]
    assert set(data) >= {"exists", "state", "last_materialized_at", "tables"}
    # No tenants/schema yet => not_provisioned, empty tables (never None).
    assert data["exists"] is False
    assert data["state"] == "not_provisioned"
    assert data["tables"] == []


# --------------------------------------------------------------------------- #
# Contract 5: server-side enforcement of injected params (do not trust the LLM)
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
async def test_get_schema_status_rejects_empty_workspace_id():
    """A missing workspace_id is rejected server-side, not silently accepted.

    The server must not trust the LLM to supply context. With an empty
    ``workspace_id`` the real handler returns a VALIDATION_ERROR envelope (07#0).
    """
    async with mcp_wire() as (_session, tools):
        raw = await tools["get_schema_status"].ainvoke({"workspace_id": ""})
    env = parse_tool_result(raw)

    assert env["success"] is False
    assert env["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.django_db(transaction=True)
async def test_run_materialization_enforces_thread_ownership_server_side(db):
    """run_materialization enforces workspace/thread membership server-side.

    With a valid-looking but unauthorized context (no membership, no thread), the
    real handler refuses rather than dispatching a job — proving the trust boundary
    lives on the server, not the LLM-facing schema (07#0). Asserted on the real
    wire end to end.
    """
    user = await _make_user("run-mat@example.com")
    ws = await Workspace.objects.acreate(name="RunMat WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)

    # workspace has no tenants and the user has no tenant membership -> NOT_FOUND,
    # and there is no thread, so the tool must NOT dispatch a materialization job.
    async with mcp_wire() as (_session, tools):
        raw = await tools["run_materialization"].ainvoke(
            {
                "workspace_id": str(ws.id),
                "user_id": str(user.id),
                "thread_id": "00000000-0000-0000-0000-000000000000",
                "tool_call_id": "call_test",
            }
        )
    env = parse_tool_result(raw)

    assert env["success"] is False
    assert env["error"]["code"] in {"NOT_FOUND", "VALIDATION_ERROR"}


# --------------------------------------------------------------------------- #
# Contract 6: end-to-end through the REAL injecting tool node
# --------------------------------------------------------------------------- #


@pytest.mark.django_db(transaction=True)
async def test_injecting_tool_node_flows_workspace_id_to_real_server(db):
    """workspace_id flows from agent state through the real injecting node to the wire.

    Builds the production injecting tool node (``_make_injecting_tool_node``) over the
    REAL wired tools and a real ``ToolNode``, runs it inside a compiled ``StateGraph``
    (so langgraph supplies the runtime the node needs), and drives it with an
    AIMessage tool call that omits ``workspace_id`` — exactly as the LLM would (the
    param is hidden from its schema). The node must inject ``workspace_id`` from state
    so the real server resolves the right workspace. No mock sits between state and
    server. This is the full chat->graph->mcp_client->MCP-tool path the suite could
    not observe.
    """
    user = await _make_user("inject@example.com")
    ws = await Workspace.objects.acreate(name="Inject WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)

    async with mcp_wire() as (_session, tools):
        base_node = ToolNode(list(tools.values()))
        injections = {
            "workspace_id": "workspace_id",
            "user_id": "user_id",
            "thread_id": "thread_id",
        }
        node = _make_injecting_tool_node(base_node, injections)

        # Run the real injecting node inside a compiled graph so langgraph provides
        # the runtime the underlying ToolNode requires.
        graph = StateGraph(AgentState)
        graph.add_node("tools", node)
        graph.set_entry_point("tools")
        graph.add_edge("tools", END)
        compiled = graph.compile()

        # LLM-shaped call: workspace_id intentionally absent from args.
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "get_schema_status", "args": {}, "id": "call_abc"}],
        )
        result_state = await compiled.ainvoke(
            {
                "messages": [ai],
                "workspace_id": str(ws.id),
                "user_id": str(user.id),
                "thread_id": "",
            }
        )

    tool_messages = [m for m in result_state["messages"] if isinstance(m, ToolMessage)]
    assert tool_messages, "injecting node produced no tool message"
    env = parse_tool_result(tool_messages[-1].content)

    # If injection failed, the server would reject empty workspace_id with
    # VALIDATION_ERROR. A successful not_provisioned envelope proves the id arrived.
    assert env["success"] is True, f"workspace_id did not reach the server: {env}"
    assert env["data"]["state"] == "not_provisioned"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _make_user(email: str):
    return await get_user_model().objects.acreate_user(email=email, password="pass")
