"""Both nested manager graphs inject scope into MCP calls and hide it from the model."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from apps.agents.tools.artifact_manager_agent import (
    ARTIFACT_MANAGER_SYSTEM_PROMPT,
    _build_artifact_manager_graph,
)
from apps.agents.tools.canvas_manager_agent import (
    CANVAS_MANAGER_SYSTEM_PROMPT,
    _build_canvas_manager_graph,
)

SUBAGENTS = [
    pytest.param(
        "apps.agents.tools.canvas_manager_agent",
        "create_canvas_tools",
        _build_canvas_manager_graph,
        CANVAS_MANAGER_SYSTEM_PROMPT,
        id="canvas",
    ),
    pytest.param(
        "apps.agents.tools.artifact_manager_agent",
        "create_artifact_graph_tools",
        _build_artifact_manager_graph,
        ARTIFACT_MANAGER_SYSTEM_PROMPT,
        id="artifact",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("prefix", "tools_factory", "builder", "prompt"), SUBAGENTS)
async def test_nested_graph_injects_scope_and_hides_it_from_the_model(
    prefix, tools_factory, builder, prompt
):
    seen: dict = {}

    @tool
    def list_datasets(workspace_id: str, user_id: str, thread_id: str, tool_call_id: str) -> str:
        """List datasets."""
        seen["mcp"] = (workspace_id, user_id, thread_id, tool_call_id)
        return "{}"

    @tool
    def local_write(note: str) -> str:
        """Local tool."""
        seen["local"] = note
        return "ok"

    unrelated_mcp = MagicMock()
    unrelated_mcp.name = "not_a_nested_tool"

    calls = AIMessage(
        content="",
        tool_calls=[
            {"id": "c1", "name": "list_datasets", "args": {}},
            {"id": "c2", "name": "local_write", "args": {"note": "hi"}},
        ],
    )
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=[calls, AIMessage(content="Done.")])
    llm = MagicMock()
    llm.bind_tools.return_value = bound

    with (
        patch(f"{prefix}.ChatAnthropic", return_value=llm),
        patch(f"{prefix}.{tools_factory}", return_value=[local_write]),
    ):
        graph = builder(SimpleNamespace(id="ws-1"), None, [list_datasets, unrelated_mcp], None)
        await graph.ainvoke(
            {
                "messages": [HumanMessage(content="go")],
                "workspace_id": "ws-1",
                "user_id": "u-1",
                "thread_id": "t-1",
            }
        )

    assert seen["mcp"] == ("ws-1", "u-1", "t-1", "c1")
    assert seen["local"] == "hi"

    (bound_tools,) = llm.bind_tools.call_args.args
    by_name = {t["function"]["name"] if isinstance(t, dict) else t.name: t for t in bound_tools}
    assert set(by_name) == {"list_datasets", "local_write"}
    assert by_name["list_datasets"]["function"]["parameters"]["properties"] == {}
    assert not isinstance(by_name["local_write"], dict)

    first_call = bound.ainvoke.await_args_list[0].args[0]
    assert isinstance(first_call[0], SystemMessage)
    assert first_call[0].content.startswith(prompt)
