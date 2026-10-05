"""Snapshot of what build_agent_graph hands the model, per role and run mode.

Pins the system prompt blocks the agent node sends, the tool schemas bound to the
model, the model call kwargs and the graph topology, so splitting the graph module
is provably behaviour-preserving. The base system prompt and artifact additions
have their own tests; they are replaced by named markers here so the snapshot
shows only the text this assembly owns. A whitespace change is a model-input change.

Regenerate after an intended change with ``UPDATE_GRAPH_SNAPSHOTS=1 uv run pytest
tests/agents/test_graph_assembly_snapshot.py`` and review the diff.
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.conf import settings
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from apps.agents.graph.base import build_agent_graph
from apps.agents.graph.prompt_context import _system_prompt_cache
from apps.agents.llm_request import MAIN_AGENT_EFFORT, chat_model_kwargs
from apps.agents.prompts.artifact_prompt import (
    ARTIFACT_PROMPT_ADDITION,
    ARTIFACT_READ_ONLY_PROMPT_ADDITION,
)
from apps.agents.prompts.base_system import select_base_system_prompt
from apps.knowledge.models import KnowledgeEntry
from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, WorkspaceTenant, WorkspaceViewSchema
from tests.tenant_access import grant_tenant_access

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "graph_assembly"
UPDATE = os.environ.get("UPDATE_GRAPH_SNAPSHOTS") == "1"

# Every MCP tool the server can expose, so the per-mode filtering is visible.
MCP_TOOLS = (
    "cancel_materialization",
    "describe_dataset",
    "describe_table",
    "get_lineage",
    "get_materialization_status",
    "get_metadata",
    "get_schema_status",
    "list_datasets",
    "list_tables",
    "list_workspaces",
    "query",
    "run_materialization",
    "semantic_catalog",
    "semantic_query",
)


class _McpArgs(BaseModel):
    request: str
    workspace_id: str = ""
    user_id: str = ""
    thread_id: str = ""
    tool_call_id: str = ""


def _mcp_tool(name: str) -> StructuredTool:
    async def call(**_kwargs) -> dict:
        return {}

    return StructuredTool.from_function(
        coroutine=call, name=name, description=f"{name} tool", args_schema=_McpArgs
    )


MODES = [
    pytest.param("write", True, "thread-1", False, True, id="chat-writer"),
    pytest.param("write", True, None, False, True, id="chat-writer-no-thread"),
    pytest.param("read", True, "thread-1", False, True, id="chat-reader"),
    pytest.param("write", False, None, False, True, id="headless-writer"),
    pytest.param("read", False, None, False, True, id="headless-reader"),
    pytest.param(None, False, None, False, True, id="headless-anonymous"),
    pytest.param("write", True, "thread-1", True, True, id="chat-writer-multi-tenant"),
    pytest.param(None, False, None, False, False, id="headless-anonymous-no-tenants"),
]


def _tool_line(schema) -> str:
    function = (schema if isinstance(schema, dict) else convert_to_openai_tool(schema))["function"]
    parameters = function.get("parameters", {})
    properties = ",".join(sorted(parameters.get("properties", {})))
    required = ",".join(sorted(parameters.get("required", [])))
    return f"{function['name']}({properties}) required=[{required}]"


def _with_markers(text: str) -> str:
    fragments = [
        (
            select_base_system_prompt(write_capable=write_capable, interactive=interactive),
            f"<<select_base_system_prompt(write_capable={write_capable}, "
            f"interactive={interactive})>>",
        )
        for write_capable in (True, False)
        for interactive in (True, False)
    ]
    fragments += [
        (ARTIFACT_PROMPT_ADDITION, "<<ARTIFACT_PROMPT_ADDITION>>"),
        (ARTIFACT_READ_ONLY_PROMPT_ADDITION, "<<ARTIFACT_READ_ONLY_PROMPT_ADDITION>>"),
    ]
    for fragment, marker in fragments:
        if fragment in text:
            assert text.count(fragment) == 1
            text = text.replace(fragment, marker)
    return text


def _render(*, system, ainvoke_kwargs, schemas, graph) -> str:
    lines = []
    for index, block in enumerate(system.content):
        extras = {k: v for k, v in block.items() if k not in {"type", "text"}}
        lines.append(f"=== system block {index} {json.dumps(extras, sort_keys=True)} ===")
        lines.append(_with_markers(block["text"]))
    lines.append(f"=== ainvoke kwargs {json.dumps(ainvoke_kwargs, sort_keys=True)} ===")
    lines.append("=== bound tools ===")
    lines.extend(_tool_line(schema) for schema in schemas)
    drawable = graph.get_graph()
    lines.append("=== nodes ===")
    lines.extend(sorted(drawable.nodes))
    lines.append("=== edges ===")
    lines.extend(
        sorted(
            f"{edge.source} -> {edge.target}{' (conditional)' if edge.conditional else ''}"
            for edge in drawable.edges
        )
    )
    return "\n".join(lines) + "\n"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("role", "interactive", "conversation_id", "multi", "tenants"), MODES)
async def test_assembled_graph_matches_snapshot(
    request,
    workspace,
    tenant,
    read_user,
    write_user,
    role,
    interactive,
    conversation_id,
    multi,
    tenants,
):
    workspace.system_prompt = "Report counts by district."
    await workspace.asave(update_fields=["system_prompt"])
    await KnowledgeEntry.objects.acreate(
        workspace=workspace, title="Active user", content="Logged a visit in the last 30 days."
    )
    if multi:
        other = await Tenant.objects.acreate(
            provider="commcare", external_id="other", canonical_name="Other"
        )
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
        await sync_to_async(grant_tenant_access)(write_user, other)
        await WorkspaceViewSchema.objects.acreate(
            workspace=workspace, schema_name="loaded_view", state=SchemaState.ACTIVE
        )
    if not tenants:
        await WorkspaceTenant.objects.filter(workspace=workspace).adelete()
    user = {"write": write_user, "read": read_user, None: None}[role]

    _system_prompt_cache.clear()
    with (
        patch("apps.agents.graph.base.ChatAnthropic") as chat,
        patch("apps.agents.graph.base.agent_date_context", return_value="<<agent_date_context>>"),
    ):
        bound = chat.return_value.bind_tools.return_value
        bound.ainvoke = AsyncMock(return_value=AIMessage(content="Done."))
        graph = await build_agent_graph(
            workspace,
            user,
            mcp_tools=[_mcp_tool(name) for name in MCP_TOOLS],
            conversation_id=conversation_id,
            interactive=interactive,
            job_id=None if interactive else 7,
        )
        await graph.ainvoke(
            {
                "messages": [HumanMessage(content="How many visits?")],
                "workspace_id": str(workspace.id),
                "user_id": str(getattr(user, "id", "")),
                "thread_id": conversation_id or "",
            }
        )

    assert chat.call_args.kwargs == {
        "model": settings.DEFAULT_LLM_MODEL,
        "max_tokens": 16_000,
        **chat_model_kwargs(MAIN_AGENT_EFFORT),
    }
    system, *_history = bound.ainvoke.call_args.args[0]
    actual = _render(
        system=system,
        ainvoke_kwargs=bound.ainvoke.call_args.kwargs,
        schemas=chat.return_value.bind_tools.call_args.args[0],
        graph=graph,
    )

    path = SNAPSHOT_DIR / f"{request.node.callspec.id}.txt"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
    assert path.exists(), f"missing snapshot {path}; run with UPDATE_GRAPH_SNAPSHOTS=1"
    assert actual == path.read_text()
