import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from apps.agents.graph.base import _make_injecting_tool_node
from apps.agents.tool_results import (
    FULL_RESULT_BUDGET_BYTES,
    TOOL_RESULT_BUDGET_BYTES,
    compact_tool_message,
)
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


def _compacted_payload(name, payload):
    message = compact_tool_message(_mcp_message(name, payload))
    assert len(message.content.encode()) <= _budget(name)
    return json.loads(message.content)


def _budget(name):
    return FULL_RESULT_BUDGET_BYTES if name == "query" else TOOL_RESULT_BUDGET_BYTES


def test_an_oversized_listing_is_cut_to_a_page_the_agent_can_continue():
    datasets = [{"name": f"ds_{i:03d}", "description": "d" * 500} for i in range(200)]
    payload = {
        "success": True,
        "data": {"datasets": datasets, "total": 200, "limit": 200, "offset": 20},
        "schema": "semantic",
    }

    data = _compacted_payload("list_datasets", payload)["data"]

    kept = data["datasets"]
    assert 0 < len(kept) < 200
    assert kept == datasets[: len(kept)]
    assert data["truncated"] is True
    assert data["has_more"] is True
    assert data["next_offset"] == 20 + len(kept)
    assert f"offset={data['next_offset']}" in data["truncation"]["hint"]


def test_a_normal_sql_result_is_not_cut():
    rows = [[i, f"worker-{i}@example.org", "x" * 200] for i in range(500)]
    payload = {"success": True, "data": {"columns": ["id", "user", "note"], "rows": rows}}

    assert len(json.dumps(payload)) > TOOL_RESULT_BUDGET_BYTES
    assert _compacted_payload("query", payload) == payload


def test_a_runaway_sql_result_keeps_whole_rows_and_says_it_was_cut():
    rows = [[i, "x" * 1_000] for i in range(500)]
    payload = {
        "success": True,
        "data": {"columns": ["id", "note"], "rows": rows, "row_count": 500, "truncated": False},
        "warnings": ["existing"],
    }

    compacted = _compacted_payload("query", payload)

    data = compacted["data"]
    assert data["columns"] == ["id", "note"]
    assert 0 < len(data["rows"]) < 500
    assert data["rows"] == rows[: len(data["rows"])]
    assert data["truncated"] is True
    assert "next_offset" not in data
    assert compacted["warnings"][0] == "existing"
    assert "rows" in compacted["warnings"][1]


def test_a_result_with_nothing_to_cut_is_replaced_by_a_note():
    payload = {"success": True, "data": {f"k{i}": i for i in range(20_000)}, "schema": "s"}

    compacted = _compacted_payload("get_metadata", payload)

    assert compacted["success"] is True
    assert "k0" not in compacted["data"]
    assert compacted["data"]["truncated"] is True


def test_oversized_plain_text_is_cut_with_a_marker():
    message = ToolMessage(content="y" * 100_000, tool_call_id="tc-1", name="get_lineage")

    content = compact_tool_message(message).content

    assert len(content.encode()) <= TOOL_RESULT_BUDGET_BYTES
    assert content.endswith("KB.]")


def test_a_page_whose_first_item_alone_is_too_large_does_not_repeat_its_offset():
    payload = {
        "success": True,
        "data": {"datasets": [{"name": "wide", "fields": ["f" * 100] * 1_000}], "offset": 30},
    }

    data = _compacted_payload("list_datasets", payload)["data"]

    assert data["datasets"] == []
    assert "next_offset" not in data
    assert "offset=30 alone is too large" in data["truncation"]["hint"]


def test_cut_sql_rows_keep_row_count_in_step_and_columns_whole():
    rows = [[i, "x" * 1_000] for i in range(500)]
    payload = {
        "success": True,
        "data": {"columns": ["id", "note"] * 200, "rows": rows, "row_count": 500},
    }

    data = _compacted_payload("query", payload)["data"]

    assert data["row_count"] == len(data["rows"]) < 500
    assert data["truncation"]["original_row_count"] == 500
    assert data["columns"] == ["id", "note"] * 200


def test_a_huge_string_is_shortened_without_losing_the_fields_beside_it():
    payload = {"success": True, "data": {"run_id": "r-1", "status": "failed", "log": "q" * 300_000}}

    data = _compacted_payload("get_materialization_status", payload)["data"]

    assert data["run_id"] == "r-1"
    assert data["status"] == "failed"
    assert data["log"].endswith("…[cut]")
    assert data["truncated"] is True


def test_many_cut_lists_still_fit_the_budget():
    payload = {"success": True, "data": {f"list_{i}": ["z" * 100] * 20 for i in range(100)}}

    data = _compacted_payload("list_tables", payload)["data"]

    assert data["truncated"] is True
    assert "and more" in data["truncation"]["hint"]


def test_a_lone_surrogate_does_not_break_the_tool_node():
    text = '{"success": true, "data": {"rows": [["\\ud800"]], "blob": "' + "b" * 60_000 + '"}}'
    message = ToolMessage(content=text, tool_call_id="tc-1", name="get_lineage")

    content = compact_tool_message(message).content

    assert len(content.encode(errors="surrogatepass")) <= TOOL_RESULT_BUDGET_BYTES
