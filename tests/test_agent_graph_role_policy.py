"""Graph tools and prompts reflect the actor's workspace capabilities."""

from unittest.mock import patch

import pytest
from langchain_core.tools import StructuredTool

from apps.agents.graph.base import (
    _build_system_prompt,
    _fetch_semantic_model_context,
    _system_prompt_cache,
    build_agent_graph,
)
from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, TenantSchema, WorkspaceTenant, WorkspaceViewSchema


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("multi", [False, True])
async def test_read_missing_catalog_preserves_loaded_sql_guidance(
    workspace, tenant, interactive, multi
):
    await TenantSchema.objects.acreate(
        tenant=tenant, schema_name="loaded", state=SchemaState.ACTIVE
    )
    if multi:
        other = await Tenant.objects.acreate(
            provider="commcare", external_id="other", canonical_name="Other"
        )
        await WorkspaceTenant.objects.acreate(workspace=workspace, tenant=other)
        await WorkspaceViewSchema.objects.acreate(
            workspace=workspace, schema_name="loaded_view", state=SchemaState.ACTIVE
        )

    context = await _fetch_semantic_model_context(
        workspace, interactive=interactive, write_capable=False
    )

    assert "Data is loaded" in context
    assert "not currently queryable" not in context
    assert "`list_tables`" in context and "`describe_table`" in context and "`query`" in context
    assert "read-write workspace role" in context
    assert "Call `run_materialization`" not in context
    if multi:
        assert "{tenant_name}__{table_name}" in context


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("writer", [False, True])
async def test_graph_resolves_live_role_for_bound_tools_and_prompt(
    workspace, read_user, write_user, interactive, writer
):
    async def query(sql: str) -> dict:
        return {}

    async def materialize() -> dict:
        return {}

    mcp_tools = [
        StructuredTool.from_function(coroutine=query, name="query", description="Query data"),
        StructuredTool.from_function(
            coroutine=materialize, name="run_materialization", description="Load data"
        ),
    ]
    _system_prompt_cache.clear()
    with (
        patch("apps.agents.graph.base.ChatAnthropic") as chat,
        patch("apps.agents.graph.base._build_system_prompt", wraps=_build_system_prompt) as prompt,
    ):
        await build_agent_graph(
            workspace,
            write_user if writer else read_user,
            interactive=interactive,
            conversation_id="thread" if interactive else None,
            mcp_tools=mcp_tools,
        )
    schemas = chat.return_value.bind_tools.call_args.args[0]
    names = {s["function"]["name"] if isinstance(s, dict) else s.name for s in schemas}
    assert "query" in names
    if interactive:
        assert "canvas_read" in names
    assert ("canvas_manager" in names) is (writer and interactive)
    assert prompt.call_args.kwargs["write_capable"] is writer
    assert prompt.call_args.kwargs["canvas_write"] is (writer and interactive)
