"""Real (unmocked) recipe runner <-> graph-build contract test (arch #238).

Exercises the REAL ``build_agent_graph`` through ``RecipeRunner.execute_async`` with
real MCP tools loaded over the in-process MCP SDK transport. Only the LLM and the
managed-data boundary are avoided: the build signature, the AgentState contract, and
the workspace_id injection path all run for real. Every other recipe test mocks
``build_agent_graph``, which is exactly why the March break (e26cd75) hid for months.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from apps.recipes.models import Recipe, RecipeRunStatus
from apps.recipes.services.runner import RecipeRunner
from mcp_server.server import mcp as scout_mcp


@pytest.fixture
def recipe(db, user, workspace):
    """Create a test recipe with variables (module-local, mirroring test_recipes.py)."""
    return Recipe.objects.create(
        workspace=workspace,
        name="Sales Analysis",
        description="Analyze sales data for a specific region and time period",
        prompt="Show me the top {{limit}} customers in {{region}} region starting from {{start_date}}",
        variables=[
            {
                "name": "region",
                "type": "select",
                "label": "Region",
                "default": "North",
                "options": ["North", "South", "East", "West"],
            },
            {
                "name": "limit",
                "type": "number",
                "label": "Number of results",
                "default": 10,
            },
            {
                "name": "start_date",
                "type": "date",
                "label": "Start Date",
            },
        ],
        is_shared=False,
        created_by=user,
    )


def _fake_llm_driving_get_schema_status():
    """A fake ChatAnthropic whose bound model:

    1. first emits a get_schema_status tool call (workspace_id omitted, as the LLM
       would — the param is hidden from its schema and injected from state);
    2. then, once it sees the ToolMessage, echoes the tool envelope as its final
       answer so the runner captures it as the response.
    """

    async def fake_ainvoke(messages, *args, **kwargs):
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        if tool_msgs:
            return AIMessage(content=str(tool_msgs[-1].content), id="ai-final")
        return AIMessage(
            content="",
            tool_calls=[{"name": "get_schema_status", "args": {}, "id": "call_1"}],
            id="ai-1",
        )

    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(side_effect=fake_ainvoke)
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_bound
    return mock_llm


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_execute_async_builds_real_graph_and_flows_workspace_id(recipe, user):
    """RecipeRunner.execute_async builds the REAL agent graph and runs a real MCP
    tool call end-to-end.

    Catches every #238 drift at once:
    - build_agent_graph is called with the real signature (drift #1) — a TypeError
      here means the runner regressed;
    - mcp_tools are loaded and attached, so get_schema_status exists (drift #3);
    - workspace_id flows from initial_state through the real injecting node into the
      real MCP server (drift #2) — proven by a not_provisioned success envelope rather
      than a VALIDATION_ERROR (which is what an empty workspace_id returns).
    """
    values = {"region": "North", "limit": 10, "start_date": "2024-01-01"}

    async with create_connected_server_and_client_session(scout_mcp) as session:
        tools = await load_mcp_tools(session)
        with (
            patch(
                "apps.recipes.services.runner.get_mcp_tools",
                new=AsyncMock(return_value=tools),
            ),
            patch(
                "apps.agents.graph.base.ChatAnthropic",
                return_value=_fake_llm_driving_get_schema_status(),
            ),
        ):
            run = await RecipeRunner(recipe=recipe, variable_values=values, user=user).execute_async()

    assert run.status == RecipeRunStatus.COMPLETED, run.step_results
    step = run.step_results[0]
    assert step["success"] is True
    assert "get_schema_status" in step["tools_used"]
    # Positive proof workspace_id reached the server: a real not_provisioned envelope,
    # never the VALIDATION_ERROR that an empty workspace_id would have produced.
    assert "not_provisioned" in step["response"]
    assert "VALIDATION_ERROR" not in step["response"]
