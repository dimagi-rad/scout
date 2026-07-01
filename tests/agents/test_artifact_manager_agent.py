import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from apps.agents.subagents.events import reset_subagent_event_queue, set_subagent_event_queue
from apps.agents.tools.artifact_manager_agent import (
    _forward_nested_event,
    _summarize_result,
    create_artifact_manager_tool,
)


@pytest.mark.asyncio
async def test_nested_local_tool_events_are_buffered_until_tool_message_id():
    import asyncio

    queue: asyncio.Queue = asyncio.Queue()
    token = set_subagent_event_queue(queue)
    try:
        run_to_tool_call_id: dict[str, str] = {}
        pending_tool_starts: dict[str, dict] = {}
        await _forward_nested_event(
            {
                "event": "on_tool_start",
                "run_id": "run-write",
                "name": "artifact_write",
                "data": {"input": {"action": "create", "title": "Dashboard"}},
            },
            "toolu_PARENT",
            run_to_tool_call_id,
            pending_tool_starts,
        )
        assert queue.empty()

        await _forward_nested_event(
            {
                "event": "on_tool_end",
                "run_id": "run-write",
                "name": "artifact_write",
                "data": {
                    "output": ToolMessage(
                        content=json.dumps({"status": "created"}),
                        tool_call_id="toolu_CHILD",
                        name="artifact_write",
                    )
                },
            },
            "toolu_PARENT",
            run_to_tool_call_id,
            pending_tool_starts,
        )

        start = await queue.get()
        end = await queue.get()
    finally:
        reset_subagent_event_queue(token)

    assert start["source"] == "subagent"
    assert start["event"]["type"] == "data-subagent-tool-input"
    assert start["event"]["data"]["toolCallId"] == "artifact_manager:toolu_CHILD"
    assert start["event"]["data"]["parentToolCallId"] == "toolu_PARENT"
    assert start["event"]["data"]["toolName"] == "artifact_write"
    assert start["event"]["data"]["input"]["action"] == "create"
    assert end["event"]["type"] == "data-subagent-tool-output"
    assert end["event"]["data"]["toolCallId"] == "artifact_manager:toolu_CHILD"
    assert end["event"]["data"]["parentToolCallId"] == "toolu_PARENT"


def test_artifact_manager_summary_is_compact():
    artifact_result = {
        "status": "created",
        "artifact": {"id": "artifact-1", "version": 2},
        "diagnostics": [],
        "manifest": {"entries": [{"block_id": "q"}, {"block_id": "chart"}]},
        "runtime": {"summary": "3/3 queries ok"},
    }
    messages = [
        ToolMessage(
            content=json.dumps(artifact_result),
            tool_call_id="toolu_WRITE",
            name="artifact_write",
        ),
        AIMessage(
            content=json.dumps(
                {
                    "status": "done",
                    "message": "Created the dashboard.",
                    "touched_blocks": ["title", "q", "chart"],
                }
            )
        ),
    ]

    summary = _summarize_result(messages, messages[-1].content)

    assert summary == {
        "status": "done",
        "artifact_id": "artifact-1",
        "artifact_version": 2,
        "touched_blocks": ["title", "q", "chart"],
        "diagnostics": [],
        "runtime_summary": "3/3 queries ok",
        "message": "Created the dashboard.",
    }


@pytest.mark.asyncio
async def test_nested_tool_output_is_truncated_with_marker():
    import asyncio

    queue: asyncio.Queue = asyncio.Queue()
    token = set_subagent_event_queue(queue)
    try:
        await _forward_nested_event(
            {
                "event": "on_tool_end",
                "run_id": "run-large",
                "name": "artifact_write",
                "data": {
                    "output": ToolMessage(
                        content="x" * 100_100,
                        tool_call_id="toolu_LARGE",
                        name="artifact_write",
                    )
                },
            },
            "toolu_PARENT",
            {},
            {},
        )

        start = await queue.get()
        end = await queue.get()
    finally:
        reset_subagent_event_queue(token)

    assert start["event"]["type"] == "data-subagent-tool-input"
    assert end["event"]["type"] == "data-subagent-tool-output"
    assert "... (truncated, 100100 chars total)" in end["event"]["data"]["output"]


@pytest.mark.asyncio
async def test_artifact_manager_parent_tool_emits_to_injected_queue(monkeypatch):
    class FakeGraph:
        async def astream_events(self, input_state, config, version):
            yield {
                "event": "on_tool_end",
                "run_id": "run-write",
                "name": "artifact_write",
                "data": {
                    "output": ToolMessage(
                        content=json.dumps(
                            {
                                "status": "created",
                                "artifact": {"id": "artifact-1", "version": 1},
                                "diagnostics": [],
                            }
                        ),
                        tool_call_id="toolu_CHILD",
                        name="artifact_write",
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
                                content=json.dumps(
                                    {
                                        "status": "created",
                                        "artifact": {"id": "artifact-1", "version": 1},
                                        "diagnostics": [],
                                    }
                                ),
                                tool_call_id="toolu_CHILD",
                                name="artifact_write",
                            ),
                            AIMessage(
                                content=json.dumps(
                                    {
                                        "status": "created",
                                        "message": "Created artifact.",
                                    }
                                )
                            ),
                        ]
                    }
                },
            }

    monkeypatch.setattr(
        "apps.agents.tools.artifact_manager_agent._build_artifact_manager_graph",
        lambda *args, **kwargs: FakeGraph(),
    )

    queue = __import__("asyncio").Queue()
    tool = create_artifact_manager_tool(
        SimpleNamespace(id="workspace-1"),
        SimpleNamespace(id="user-1"),
        [],
        conversation_id="thread-1",
    )
    await tool.ainvoke(
        {
            "task": "create",
            "tool_call_id": "toolu_PARENT",
            "subagent_event_queue": queue,
        }
    )

    start = await queue.get()
    end = await queue.get()
    assert start["event"]["type"] == "data-subagent-tool-input"
    assert start["event"]["data"]["parentToolCallId"] == "toolu_PARENT"
    assert end["event"]["type"] == "data-subagent-tool-output"
