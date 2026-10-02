import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from apps.agents.graph.base import _make_injecting_tool_node
from apps.agents.tool_results import compact_tool_message
from apps.agents.tools import artifact_manager_agent, canvas_manager_agent

ENVELOPE = {"success": True, "data": {"datasets": [{"name": "visits", "label": "Visits é"}]}}


def _mcp_message(name: str, payload=ENVELOPE) -> ToolMessage:
    """A ToolMessage as langchain_mcp_adapters builds it from a FastMCP result."""
    return ToolMessage(
        content=[{"type": "text", "text": json.dumps(payload, indent=2), "id": "lc_1"}],
        artifact={"structured_content": payload},
        tool_call_id="tc-1",
        name=name,
    )


def _base_node(messages):
    node = MagicMock()
    node.ainvoke = AsyncMock(return_value={"messages": messages})
    return node


def _state(tool_name):
    call = {"name": tool_name, "id": "tc-1", "args": {}}
    return {
        "messages": [AIMessage(content="", tool_calls=[call])],
        "workspace_id": "ws-1",
        "user_id": "user-1",
        "thread_id": "thread-1",
    }


def test_compact_tool_message_drops_whitespace_and_the_duplicate_artifact():
    compacted = compact_tool_message(_mcp_message("list_datasets"))

    assert (
        compacted.content
        == '{"success":true,"data":{"datasets":[{"name":"visits","label":"Visits é"}]}}'
    )
    assert json.loads(compacted.content) == ENVELOPE
    assert compacted.artifact is None
    assert compacted.tool_call_id == "tc-1"
    assert compacted.name == "list_datasets"


def test_compact_tool_message_keeps_non_json_text():
    message = ToolMessage(content="Error: boom", tool_call_id="tc-1", name="query", status="error")

    compacted = compact_tool_message(message)

    assert compacted.content == "Error: boom"
    assert compacted.status == "error"


def test_compact_tool_message_leaves_multi_block_content_alone():
    blocks = [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}]
    message = ToolMessage(content=blocks, tool_call_id="tc-1", name="query")

    assert compact_tool_message(message).content == blocks


@pytest.mark.asyncio
async def test_agent_tool_node_compacts_mcp_results_only():
    mcp = _mcp_message("list_datasets")
    local = ToolMessage(content='{\n  "ok": true\n}', tool_call_id="tc-2", name="save_learning")
    node = _make_injecting_tool_node(_base_node([mcp, local]), {})

    result = await node(_state("list_datasets"))

    compacted, untouched = result["messages"]
    assert compacted.content == json.dumps(ENVELOPE, separators=(",", ":"), ensure_ascii=False)
    assert untouched is local


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [canvas_manager_agent, artifact_manager_agent])
async def test_subagent_tool_nodes_compact_mcp_results(module):
    node = module._make_nested_tool_node(_base_node([_mcp_message("list_datasets")]))

    result = await node(_state("list_datasets"))

    assert "\n" not in result["messages"][0].content
    assert result["messages"][0].artifact is None
