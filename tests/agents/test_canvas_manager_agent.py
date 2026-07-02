import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from apps.agents.subagents.events import reset_subagent_event_queue, set_subagent_event_queue
from apps.agents.subagents.forwarding import NestedEventForwarder
from apps.agents.tools.canvas_manager_agent import (
    _summarize_result,
    create_canvas_manager_tool,
)


def test_canvas_manager_summary_prefers_final_json_and_commit_truth():
    commit_result = {
        "committed": [{"object_type": "field", "name": "total_amount", "change_type": "create"}],
        "blocked": False,
        "conflicts": [],
        "blocking_diagnostics": [],
    }
    messages = [
        ToolMessage(
            content=json.dumps(commit_result),
            tool_call_id="toolu_COMMIT",
            name="canvas_commit",
        ),
        AIMessage(
            content=json.dumps(
                {
                    "status": "done",
                    "message": "Added the total_amount measure and saved.",
                    "changes": ["field/raw_visits.total_amount — new"],
                    "diagnostics": [],
                    "committed": True,
                }
            )
        ),
    ]

    summary = _summarize_result(messages)

    assert summary["status"] == "done"
    assert summary["committed"] is True
    assert summary["changes"] == ["field/raw_visits.total_amount — new"]
    assert summary["diagnostics"] == []
    assert "total_amount" in summary["message"]


def test_canvas_manager_summary_falls_back_to_tool_diagnostics():
    apply_result = {
        "applied": [{"op": "create", "object": "field/abc"}],
        "diagnostics": [{"code": "UNKNOWN_COLUMN", "severity": "error"}],
        "can_commit": False,
    }
    messages = [
        ToolMessage(
            content=json.dumps(apply_result),
            tool_call_id="toolu_APPLY",
            name="canvas_apply",
        ),
        AIMessage(content="could not finish"),
    ]

    summary = _summarize_result(messages)

    assert summary["committed"] is False
    assert summary["diagnostics"][0]["code"] == "UNKNOWN_COLUMN"


@pytest.mark.asyncio
async def test_forwarder_buffers_local_tool_start_until_tool_message_id():
    queue: asyncio.Queue = asyncio.Queue()
    token = set_subagent_event_queue(queue)
    try:
        forwarder = NestedEventForwarder("canvas_manager", "toolu_PARENT")
        await forwarder.forward(
            {
                "event": "on_tool_start",
                "run_id": "run-apply",
                "name": "canvas_apply",
                "data": {"input": {"operations": [{"op": "add_existing"}]}},
            }
        )
        assert queue.empty()
        await forwarder.forward(
            {
                "event": "on_tool_end",
                "run_id": "run-apply",
                "name": "canvas_apply",
                "data": {
                    "output": ToolMessage(
                        content=json.dumps({"applied": []}),
                        tool_call_id="toolu_CHILD",
                        name="canvas_apply",
                    )
                },
            }
        )
        start = await queue.get()
        end = await queue.get()
    finally:
        reset_subagent_event_queue(token)

    assert start["event"]["type"] == "data-subagent-tool-input"
    assert start["event"]["data"]["toolCallId"] == "canvas_manager:toolu_CHILD"
    assert start["event"]["data"]["subagentName"] == "canvas_manager"
    assert end["event"]["type"] == "data-subagent-tool-output"
    assert forwarder.trace()["events"] == [start["event"], end["event"]]


@pytest.mark.asyncio
async def test_canvas_manager_parent_tool_emits_to_injected_queue(monkeypatch):
    class FakeGraph:
        async def astream_events(self, input_state, config, version):
            yield {
                "event": "on_tool_end",
                "run_id": "run-commit",
                "name": "canvas_commit",
                "data": {
                    "output": ToolMessage(
                        content=json.dumps({"committed": [{"name": "x"}], "blocked": False}),
                        tool_call_id="toolu_CHILD",
                        name="canvas_commit",
                    )
                },
            }
            yield {
                "event": "on_chain_end",
                "name": "agent",
                "data": {
                    "output": {
                        "messages": [
                            ToolMessage(
                                content=json.dumps({"committed": [{"name": "x"}]}),
                                tool_call_id="toolu_CHILD",
                                name="canvas_commit",
                            ),
                            AIMessage(
                                content=json.dumps(
                                    {
                                        "status": "done",
                                        "message": "Committed.",
                                        "committed": True,
                                    }
                                )
                            ),
                        ]
                    }
                },
            }

    monkeypatch.setattr(
        "apps.agents.tools.canvas_manager_agent._build_canvas_manager_graph",
        lambda *args, **kwargs: FakeGraph(),
    )

    queue: asyncio.Queue = asyncio.Queue()
    tool = create_canvas_manager_tool(
        SimpleNamespace(id="workspace-1"),
        SimpleNamespace(id="user-1"),
        [],
        conversation_id="thread-1",
    )
    result = await tool.ainvoke(
        {
            "task": "add a measure and commit",
            "tool_call_id": "toolu_PARENT",
            "subagent_event_queue": queue,
        }
    )

    status = await queue.get()
    start = await queue.get()
    end = await queue.get()
    assert status["event"]["type"] == "data-subagent-status"
    assert status["event"]["data"]["subagentName"] == "canvas_manager"
    assert start["event"]["type"] == "data-subagent-tool-input"
    assert start["event"]["data"]["parentToolCallId"] == "toolu_PARENT"
    assert end["event"]["type"] == "data-subagent-tool-output"
    assert result["status"] == "done"
    assert result["committed"] is True
    assert result["subagent_trace"]["events"][0]["type"] == "data-subagent-status"
