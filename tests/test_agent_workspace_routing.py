from unittest.mock import AsyncMock

import pytest


def test_agent_state_has_workspace_id_field():
    from apps.agents.graph.state import AgentState

    assert "workspace_id" in AgentState.__annotations__


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_injecting_node_includes_workspace_id():
    """The injecting node must inject workspace_id into MCP tool calls."""
    from langchain_core.messages import AIMessage

    from apps.agents.graph.base import _make_injecting_tool_node

    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "query",
                        "args": {"tenant_id": "old-tenant", "sql": "SELECT 1"},
                    }
                ],
            )
        ],
        "tenant_id": "old-tenant",
        "tenant_membership_id": "membership-123",
        "workspace_id": "ws-uuid-456",
        "user_id": "user-1",
        "user_role": "analyst",
        "needs_correction": False,
        "retry_count": 0,
        "correction_context": {},
        "tenant_name": "Old Tenant",
    }

    mock_base_node = AsyncMock()
    mock_base_node.ainvoke.return_value = {"messages": []}

    node = _make_injecting_tool_node(
        mock_base_node,
        injections={
            "tenant_id": "tenant_id",
            "tenant_membership_id": "tenant_membership_id",
            "workspace_id": "workspace_id",
        },
    )

    await node(state)

    call_args = mock_base_node.ainvoke.call_args[0][0]
    last_msg = call_args["messages"][-1]
    assert last_msg.tool_calls[0]["args"]["workspace_id"] == "ws-uuid-456"
