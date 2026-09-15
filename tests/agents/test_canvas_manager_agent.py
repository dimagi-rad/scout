import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError

from apps.agents.subagents.events import (
    get_subagent_event_queue,
    reset_subagent_event_queue,
    set_subagent_event_queue,
)
from apps.agents.subagents.forwarding import NestedEventForwarder
from apps.agents.tools.canvas_manager_agent import (
    CANVAS_MANAGER_SYSTEM_PROMPT,
    NESTED_RECURSION_LIMIT,
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


def test_canvas_manager_prompt_teaches_dataset_edit_capabilities():
    prompt = CANVAS_MANAGER_SYSTEM_PROMPT

    assert "ADD new dimensions and measures" in prompt
    assert "Field value format / display format" in prompt
    assert "`format`" in prompt
    assert "`currency`" in prompt
    assert "CTE datasets" in prompt
    assert "decimal_02" in prompt
    assert "Calculated and ratio measures" in prompt
    assert 'measure_type: "number"' in prompt
    assert "approved_visit_count" in prompt
    assert "`cube_sql`" in prompt
    assert "{approved_visit_count}::numeric / NULLIF({count}, 0)" in prompt
    assert "row-level calculation" in prompt
    assert "Calculated dimensions" in prompt
    assert "preserve row grain" in prompt
    assert "`filters` remains a measure-only option" in prompt


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
    assert not queue.empty()
    completed = queue.get_nowait()
    assert completed["event"]["data"]["phase"] == "completed"
    assert get_subagent_event_queue() is None


def test_canvas_manager_prompt_explains_pending_dataset_lifecycle():
    assert "Do NOT recreate these fields" in CANVAS_MANAGER_SYSTEM_PROMPT
    assert "create the dataset WITHOUT field" in CANVAS_MANAGER_SYSTEM_PROMPT
    assert "If only staging is authorized" in CANVAS_MANAGER_SYSTEM_PROMPT
    assert "report blocked with the remaining diagnostics" in CANVAS_MANAGER_SYSTEM_PROMPT
    assert "Reusable keyword-rule SQL is NOT a fixed snapshot" in CANVAS_MANAGER_SYSTEM_PROMPT


@pytest.mark.parametrize("claimed_commit", [False, True])
def test_canvas_manager_commit_truth_does_not_trust_model_claim(claimed_commit):
    model_summary = AIMessage(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "status": "done",
                        "message": "Finished.",
                        "committed": claimed_commit,
                    }
                ),
            }
        ]
    )
    assert _summarize_result([model_summary])["committed"] is False
    confirmed = ToolMessage(
        name="canvas_commit",
        tool_call_id="commit-1",
        content=json.dumps({"committed": [{"object_type": "dataset", "name": "topics"}]}),
    )
    noop = ToolMessage(
        name="canvas_commit",
        tool_call_id="commit-2",
        content=json.dumps({"committed": [], "blocked": False}),
    )
    result = _summarize_result([confirmed, noop, model_summary])
    assert result["committed"] is True
    assert result["committed_objects"] == [{"object_type": "dataset", "name": "topics"}]
    assert result["pending_count"] == 0
    assert result["message"] == "Finished."


@pytest.mark.asyncio
@pytest.mark.parametrize("cube_ok", [False, True])
async def test_real_nested_graph_step_limit_preserves_partial_commit_and_closes_lifecycle(
    monkeypatch, cube_ok
):
    @tool
    async def canvas_commit() -> dict:
        """Commit the staged dataset."""
        return {
            "committed": [{"object_type": "dataset", "name": "topics", "change_type": "create"}],
            "blocked": False,
            "cube_schema": {"ok": cube_ok},
        }

    @tool
    async def canvas_apply() -> dict:
        """Stage a remaining field edit."""
        return {
            "applied": [{"op": "set", "target": "field/topics.topic/description"}],
            "diagnostics": [{"code": "UNKNOWN_COLUMN", "severity": "error"}],
            "pending_count": 1,
            "can_commit": False,
        }

    class LoopingModel:
        calls = 0

        def bind_tools(self, schemas):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            name = "canvas_commit" if self.calls == 1 else "canvas_apply"
            return AIMessage(
                content="Still working.",
                tool_calls=[{"id": f"call-{self.calls}", "name": name, "args": {}}],
            )

    model = LoopingModel()
    monkeypatch.setattr(
        "apps.agents.tools.canvas_manager_agent.ChatAnthropic", lambda **kwargs: model
    )
    monkeypatch.setattr(
        "apps.agents.tools.canvas_manager_agent.create_canvas_tools",
        lambda *args: [canvas_apply, canvas_commit],
    )
    queue = asyncio.Queue()
    manager = create_canvas_manager_tool(SimpleNamespace(id="ws"), None, [], "thread")

    result = await manager.ainvoke(
        {"task": "Create topics and commit", "subagent_event_queue": queue}
    )

    assert NESTED_RECURSION_LIMIT == 18
    assert model.calls == 9
    assert result["status"] == "error"
    assert result["error_code"] == "STEP_LIMIT_REACHED"
    assert result["committed"] is True
    assert result["committed_objects"][0]["name"] == "topics"
    assert result["cube_schema"] == {"ok": cube_ok}
    assert result["pending_count"] == 1
    assert result["diagnostics"] == [{"code": "UNKNOWN_COLUMN", "severity": "error"}]
    assert "were not rolled back" in result["message"]
    events = [queue.get_nowait()["event"] for _ in range(queue.qsize())]
    assert events[-1]["data"]["phase"] == "failed"
    assert events[-1]["data"]["committed"] is True
    assert any(event["type"] == "data-subagent-error" for event in events)
    assert result["subagent_trace"]["events"][0]["data"]["phase"] == "failed"
    assert get_subagent_event_queue() is None


def test_canvas_manager_summary_preserves_cube_failure_after_commit():
    result = _summarize_result(
        [
            ToolMessage(
                name="canvas_commit",
                tool_call_id="commit-1",
                content=json.dumps(
                    {
                        "committed": [{"object_type": "dataset", "name": "topics"}],
                        "blocked": False,
                        "cube_schema": {"ok": False, "error": "validator failed"},
                    }
                ),
            ),
            AIMessage(content=json.dumps({"status": "done", "message": "Saved and queryable."})),
        ]
    )
    assert result["committed"] is True
    assert result["status"] == "error"
    assert result["cube_schema"]["error"] == "validator failed"
    assert "promotion failed" in result["message"]


@pytest.mark.parametrize(
    ("later_report", "expected_status", "expected_code"),
    [
        (
            {
                "committed": [],
                "blocked": True,
                "blocking_diagnostics": [{"code": "UNKNOWN_COLUMN", "severity": "error"}],
            },
            "blocked",
            "UNKNOWN_COLUMN",
        ),
        (
            {"committed": [], "blocked": False, "conflicts": [{"code": "CONFLICT"}]},
            "blocked",
            "CONFLICT",
        ),
        ({"errors": [{"code": "FORBIDDEN"}]}, "error", "FORBIDDEN"),
    ],
)
@pytest.mark.parametrize("prior_commit", [False, True])
def test_canvas_manager_latest_failed_commit_overrides_success_prose(
    later_report, expected_status, expected_code, prior_commit
):
    messages = []
    committed = [{"object_type": "dataset", "name": "topics"}]
    if prior_commit:
        messages.append(
            ToolMessage(
                name="canvas_commit",
                tool_call_id="commit-1",
                content=json.dumps(
                    {"committed": committed, "blocked": False, "cube_schema": {"ok": True}}
                ),
            )
        )
    messages.extend(
        [
            ToolMessage(
                name="canvas_apply",
                tool_call_id="apply-2",
                content=json.dumps(
                    {
                        "applied": [{"op": "create"}],
                        "diagnostics": [{"code": "UNKNOWN_COLUMN", "severity": "error"}],
                        "pending_count": 1,
                        "can_commit": False,
                    }
                ),
            ),
            ToolMessage(
                name="canvas_commit", tool_call_id="commit-2", content=json.dumps(later_report)
            ),
            AIMessage(
                content=json.dumps(
                    {"status": "done", "message": "All changes committed and queryable."}
                )
            ),
        ]
    )

    result = _summarize_result(messages)

    assert result["status"] == expected_status
    assert result["committed"] is prior_commit
    assert result["committed_objects"] == (committed if prior_commit else [])
    assert result["pending_count"] == 1
    assert expected_code in {item["code"] for item in result["diagnostics"]}
    assert "All changes committed" not in result["message"]
    if prior_commit:
        assert "were not rolled back" in result["message"]
        assert result["cube_schema"] == {"ok": True}
    else:
        assert "No successful commit was observed" in result["message"]


def test_canvas_manager_later_success_clears_resolved_commit_failure():
    result = _summarize_result(
        [
            ToolMessage(
                name="canvas_commit",
                tool_call_id="commit-1",
                content=json.dumps({"committed": [], "blocked": True}),
            ),
            ToolMessage(
                name="canvas_commit",
                tool_call_id="commit-2",
                content=json.dumps(
                    {
                        "committed": [{"name": "topics"}],
                        "blocked": False,
                        "cube_schema": {"ok": True},
                    }
                ),
            ),
            AIMessage(content=json.dumps({"status": "done", "message": "Saved."})),
        ]
    )

    assert result["status"] == "done"
    assert result["pending_count"] == 0
    assert result["diagnostics"] == []


def test_canvas_manager_blocked_commit_does_not_claim_zero_pending_changes():
    result = _summarize_result(
        [
            ToolMessage(
                name="canvas_commit",
                tool_call_id="commit-1",
                content=json.dumps({"committed": [{"name": "topics"}], "blocked": False}),
            ),
            ToolMessage(
                name="canvas_commit",
                tool_call_id="commit-2",
                content=json.dumps({"committed": [], "blocked": True}),
            ),
        ]
    )

    assert result["status"] == "blocked"
    assert result["committed"] is True
    assert result["pending_count"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_report",
    [
        {"committed": [{"name": "topics"}], "blocked": False, "cube_schema": {"ok": False}},
        {"committed": [], "blocked": True},
        {"errors": [{"code": "FORBIDDEN"}]},
    ],
)
async def test_canvas_manager_failed_result_closes_lifecycle_as_failed(monkeypatch, commit_report):
    class FailedGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {
                "event": "on_tool_end",
                "name": "canvas_commit",
                "run_id": "commit-1",
                "data": {
                    "output": ToolMessage(
                        name="canvas_commit",
                        tool_call_id="commit-1",
                        content=json.dumps(commit_report),
                    )
                },
            }
            yield {
                "event": "on_chain_end",
                "name": "agent",
                "data": {
                    "output": {
                        "messages": [
                            AIMessage(content=json.dumps({"status": "done", "message": "Saved."}))
                        ]
                    }
                },
            }

    monkeypatch.setattr(
        "apps.agents.tools.canvas_manager_agent._build_canvas_manager_graph",
        lambda *_args: FailedGraph(),
    )
    queue = asyncio.Queue()
    manager = create_canvas_manager_tool(SimpleNamespace(id="ws"), None, [], "thread")

    result = await manager.ainvoke({"task": "Commit", "subagent_event_queue": queue})

    assert result["status"] in {"blocked", "error"}
    events = [queue.get_nowait()["event"] for _ in range(queue.qsize())]
    assert events[-1]["data"]["phase"] == "failed"
    assert events[-1]["data"]["message"] == result["message"]
    assert result["subagent_trace"]["events"][0]["data"]["phase"] == "failed"
    assert get_subagent_event_queue() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_started", [False, True])
async def test_canvas_manager_failure_before_confirmed_commit_is_truthful(
    monkeypatch, commit_started
):
    class FailingGraph:
        async def astream_events(self, input_state, config, version):
            if commit_started:
                yield {"event": "on_tool_start", "name": "canvas_commit", "run_id": "commit-1"}
                raise RuntimeError("internal exception detail must stay in server logs")
            raise GraphRecursionError("step budget exhausted")

    monkeypatch.setattr(
        "apps.agents.tools.canvas_manager_agent._build_canvas_manager_graph",
        lambda *args: FailingGraph(),
    )
    queue = asyncio.Queue()
    manager = create_canvas_manager_tool(SimpleNamespace(id="ws"), None, [], "thread")
    result = await manager.ainvoke({"task": "Edit the canvas", "subagent_event_queue": queue})

    assert result["status"] == "error"
    assert result["committed"] is (None if commit_started else False)
    assert result["commit_unconfirmed"] is commit_started
    assert result["pending_count"] is None
    assert "internal exception detail" not in result["message"]
    assert "before continuing" in result["message"]
    assert get_subagent_event_queue() is None
    assert (
        list(queue.get_nowait()["event"] for _ in range(queue.qsize()))[-1]["data"]["phase"]
        == "failed"
    )


@pytest.mark.asyncio
async def test_canvas_manager_build_failure_resets_queue(monkeypatch):
    def fail_build(*args):
        raise RuntimeError("invalid graph")

    monkeypatch.setattr(
        "apps.agents.tools.canvas_manager_agent._build_canvas_manager_graph", fail_build
    )
    queue = asyncio.Queue()
    manager = create_canvas_manager_tool(SimpleNamespace(id="ws"), None, [], "thread")
    result = await manager.ainvoke({"task": "Edit the canvas", "subagent_event_queue": queue})

    assert result["error_code"] == "CANVAS_MANAGER_FAILED"
    assert get_subagent_event_queue() is None
