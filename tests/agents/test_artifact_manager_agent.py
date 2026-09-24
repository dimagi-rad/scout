import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from apps.agents.subagents.events import reset_subagent_event_queue, set_subagent_event_queue
from apps.agents.tools.artifact_manager_agent import (
    ARTIFACT_MANAGER_TASK_REQUIRED_MESSAGE,
    _artifact_manager_failure_result,
    _extract_final_text,
    _forward_nested_event,
    _SubagentTraceRecorder,
    _summarize_result,
    create_artifact_manager_tool,
)
from apps.workspaces.access import tool_write_denied


@pytest.mark.asyncio
async def test_nested_local_tool_events_are_buffered_until_tool_message_id():
    import asyncio

    queue: asyncio.Queue = asyncio.Queue()
    token = set_subagent_event_queue(queue)
    try:
        run_to_tool_call_id: dict[str, str] = {}
        pending_tool_starts: dict[str, dict] = {}
        message_buffers: dict[tuple[str, str], str] = {}
        trace = _SubagentTraceRecorder()
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
            message_buffers,
            trace,
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
            message_buffers,
            trace,
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
    assert trace.to_dict()["events"] == [start["event"], end["event"]]


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


def _topic_requirement():
    return {
        "kind": "dataset",
        "need": "Reviewed topics with unmatched messages kept unclassified",
        "source_datasets": ["raw_messages"],
        "source_members": ["raw_messages.content", "raw_messages.message_id"],
        "grain": "One message per tenant and reviewed snapshot",
        "decisions": ["User must approve classification method and coverage"],
    }


def test_metadata_only_summary_does_not_claim_data_was_verified():
    message = ToolMessage(
        name="artifact_write",
        tool_call_id="metadata-edit",
        content=json.dumps(
            {
                "status": "updated",
                "runtime": None,
                "runtime_validation": "not_required_metadata_only",
            }
        ),
    )
    summary = _summarize_result([message], "Description saved.")
    assert summary["runtime_summary"] == "Metadata-only edit; data was not revalidated."


def _write_message(result, call_id="write"):
    return ToolMessage(name="artifact_write", tool_call_id=call_id, content=json.dumps(result))


def _published_artifact_result(artifact_id, **overrides):
    return {
        "status": "created",
        "artifact": {"id": artifact_id, "version": 1},
        "runtime": {"success": True, "summary": "2/2 queries ok"},
        **overrides,
    }


def _final_message(payload, content_blocks):
    text = json.dumps(payload)
    return AIMessage(content=[{"type": "text", "text": text}] if content_blocks else text)


def test_final_text_joins_text_blocks_without_including_reasoning_or_tool_content():
    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "Ignored non-text content", "signature": "test"},
            "```json\n",
            {"type": "text", "text": '{"status":'},
            {"type": "text", "text": '"done"}'},
            "\n```",
            {"type": "tool_use", "id": "tool", "name": "unused", "input": {}},
        ]
    )
    assert _extract_final_text([message]) == '```json\n{"status":"done"}\n```'


def test_non_text_final_message_does_not_revive_an_earlier_model_proposal():
    earlier = _final_message({"status": "needs_data_model"}, content_blocks=True)
    final = AIMessage(content=[{"type": "thinking", "thinking": "No final response"}])
    assert _extract_final_text([earlier, final]) == ""


def test_selected_deliverable_survives_cleanup_of_another_artifact():
    deliverable = _published_artifact_result("deliverable")
    cleanup = _published_artifact_result(
        "probe-v2",
        status="updated",
        previous_artifact_id="probe-v1",
        runtime=None,
        runtime_validation="not_required_metadata_only",
    )
    summary = _summarize_result(
        [_write_message(deliverable), _write_message(cleanup, "cleanup")],
        json.dumps({"status": "done", "artifact_id": "deliverable"}),
    )
    assert summary["artifact_id"] == "deliverable"
    assert summary["artifact_version"] == 1
    assert summary["runtime_summary"] == "2/2 queries ok"


@pytest.mark.parametrize("requested_id", [None, "", "invented", [], {"id": "deliverable"}])
def test_unverified_deliverable_selection_keeps_latest_tool_result(requested_id):
    summary = _summarize_result(
        [
            _write_message({"status": "error", "message": "Earlier attempt failed."}, "failed"),
            _write_message(_published_artifact_result("saved")),
        ],
        json.dumps({"status": "done", "artifact_id": requested_id}),
    )
    assert summary["artifact_id"] == "saved"
    assert summary["status"] == "done"


@pytest.mark.parametrize("status", ["updated", "replaced"])
def test_selected_predecessor_cannot_override_a_later_published_revision(status):
    summary = _summarize_result(
        [
            _write_message(_published_artifact_result("v1")),
            _write_message(
                _published_artifact_result("v2", status=status, previous_artifact_id="v1"),
                "update",
            ),
        ],
        json.dumps({"status": "done", "artifact_id": "v1"}),
    )
    assert summary["artifact_id"] == "v2"


def test_selected_revision_follows_successors_without_switching_to_cleanup():
    summary = _summarize_result(
        [
            _write_message(_published_artifact_result("v1")),
            _write_message(
                _published_artifact_result("v2", status="updated", previous_artifact_id="v1"),
                "update-1",
            ),
            _write_message(
                _published_artifact_result("v3", status="replaced", previous_artifact_id="v2"),
                "update-2",
            ),
            _write_message(_published_artifact_result("v1", status="checked"), "old-check"),
            _write_message(_published_artifact_result("other"), "cleanup"),
        ],
        json.dumps({"status": "done", "artifact_id": "v1"}),
    )
    assert summary["artifact_id"] == "v3"


def test_successful_deliverable_after_a_failed_attempt_remains_selected():
    summary = _summarize_result(
        [
            _write_message({"status": "error", "message": "Earlier attempt failed."}, "failed"),
            _write_message(_published_artifact_result("deliverable")),
            _write_message(_published_artifact_result("other"), "cleanup"),
        ],
        json.dumps({"status": "done", "artifact_id": "deliverable"}),
    )
    assert summary["status"] == "done"
    assert summary["artifact_id"] == "deliverable"


@pytest.mark.parametrize("status", ["error", "denied", "checked"])
@pytest.mark.parametrize("cleanup_after_failure", [False, True])
def test_deliverable_selection_cannot_hide_a_later_failure(status, cleanup_after_failure):
    failure = {
        "status": status,
        "message": "Access denied",
        "runtime": {
            "success": False,
            "failures": [{"category": "permission_required", "message": "Access denied"}],
        },
    }
    messages = [
        _write_message(_published_artifact_result("deliverable")),
        _write_message(failure, "failure"),
    ]
    if cleanup_after_failure:
        messages.append(_write_message(_published_artifact_result("other"), "cleanup"))
    summary = _summarize_result(
        messages, json.dumps({"status": "done", "artifact_id": "deliverable"})
    )
    assert summary["status"] == "error"
    assert summary["artifact_id"] is None
    assert summary["runtime_failures"][0]["category"] == "permission_required"


def test_deliverable_selection_uses_latest_check_of_the_same_artifact():
    summary = _summarize_result(
        [
            _write_message(_published_artifact_result("deliverable")),
            _write_message(
                _published_artifact_result(
                    "deliverable", status="checked", runtime={"success": True, "summary": "Fresh"}
                ),
                "check",
            ),
            _write_message(_published_artifact_result("other"), "cleanup"),
        ],
        json.dumps({"status": "done", "artifact_id": "deliverable"}),
    )
    assert summary["artifact_id"] == "deliverable"
    assert summary["runtime_summary"] == "Fresh"


@pytest.mark.parametrize("cleanup_after_invalid", [False, True])
def test_unparseable_write_never_substitutes_an_unrelated_artifact(cleanup_after_invalid):
    messages = [
        _write_message(_published_artifact_result("deliverable")),
        ToolMessage(name="artifact_write", tool_call_id="broken", content="not JSON"),
    ]
    if cleanup_after_invalid:
        messages.append(_write_message(_published_artifact_result("other"), "cleanup"))
    summary = _summarize_result(
        messages,
        json.dumps({"status": "done", "artifact_id": "deliverable"}),
    )
    assert summary["artifact_id"] is None
    assert summary["status"] == "error"
    assert "invalid write result" in summary["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content_blocks", [False, True], ids=["string", "provider_text_blocks"])
async def test_selected_deliverable_is_emitted_in_parent_result_and_preview_event(
    monkeypatch, content_blocks
):
    class FakeGraph:
        async def astream_events(self, input_state, config, version):
            yield {
                "event": "on_chain_end",
                "data": {
                    "output": {
                        "messages": [
                            _write_message(_published_artifact_result("deliverable")),
                            _write_message(_published_artifact_result("other"), "cleanup"),
                            _final_message(
                                {"status": "done", "artifact_id": "deliverable"}, content_blocks
                            ),
                        ]
                    }
                },
            }

    monkeypatch.setattr(
        "apps.agents.tools.artifact_manager_agent._build_artifact_manager_graph",
        lambda *args, **kwargs: FakeGraph(),
    )
    manager = create_artifact_manager_tool(SimpleNamespace(id="workspace"), None, [])
    result = await manager.ainvoke({"task": "Create the deliverable."})

    assert result["artifact_id"] == "deliverable"
    completed = result["subagent_trace"]["events"][-1]
    assert completed["type"] == "data-subagent-status"
    assert completed["data"]["phase"] == "completed"
    assert completed["data"]["artifactId"] == "deliverable"


def test_artifact_manager_returns_missing_topic_model_to_parent_without_artifact():
    response = {
        "status": "needs_data_model",
        "message": "Topic labels must be saved before this chart can query them.",
        "data_requirements": [_topic_requirement()],
    }
    message = AIMessage(content=json.dumps(response))

    summary = _summarize_result([message], message.content)

    assert summary["status"] == "needs_data_model"
    assert summary["data_requirements"] == response["data_requirements"]
    assert summary["artifact_id"] is None
    assert summary["artifact_version"] is None
    assert summary["runtime_summary"] == ""


@pytest.mark.parametrize(
    "requirements",
    [
        None,
        [],
        ["create a dataset"],
        [{}],
        [_topic_requirement()] * 9,
        [{**_topic_requirement(), "need": "x" * 501}],
        [{**_topic_requirement(), "grain": "  "}],
        [{**_topic_requirement(), "source_datasets": []}],
        [{**_topic_requirement(), "kind": "materialize"}],
        [{**_topic_requirement(), "user_approved": True}],
    ],
)
def test_artifact_manager_rejects_invalid_data_model_handoff(requirements):
    response = {
        "status": "needs_data_model",
        "data_requirements": requirements,
    }

    summary = _summarize_result([], json.dumps(response))

    assert summary["status"] == "invalid_data_requirements"
    assert "data_requirements" not in summary
    assert "no model change is authorized" in summary["message"]
    assert 1 <= len(summary["requirement_errors"]) <= 8
    assert all(set(error) == {"path", "code", "message"} for error in summary["requirement_errors"])
    assert all(error["path"] for error in summary["requirement_errors"])


def test_invalid_handoff_keeps_bounded_gap_description_and_missing_field_details():
    summary = _summarize_result(
        [],
        json.dumps(
            {
                "status": "needs_data_model",
                "message": "No topic field is available. " + "x" * 1500,
                "data_requirements": [{}],
            }
        ),
    )
    assert summary["status"] == "invalid_data_requirements"
    assert summary["subagent_message"].startswith("No topic field is available.")
    assert len(summary["subagent_message"]) == 1200
    assert len(summary["requirement_errors"]) == 6
    assert "data_requirements" not in summary


@pytest.mark.parametrize(
    "status",
    ["needs_data_model", "done", [], None],
    ids=["model_proposal", "false_success", "malformed_status", "missing_status"],
)
def test_permission_denial_cannot_be_replaced_by_a_model_proposal(status):
    denied = tool_write_denied()
    summary = _summarize_result(
        [ToolMessage(name="artifact_write", tool_call_id="write", content=json.dumps(denied))],
        json.dumps(
            {
                "status": status,
                "message": "Create a different model to fix this.",
                "touched_blocks": ["invented_block"],
                "data_requirements": [_topic_requirement()],
            }
        ),
    )
    assert summary["status"] == "error"
    assert summary["message"] == denied["message"]
    assert summary["artifact_id"] is None
    assert summary["artifact_version"] is None
    assert summary["touched_blocks"] == []
    assert "data_requirements" not in summary
    assert "requirement_errors" not in summary
    assert "subagent_message" not in summary
    assert summary["runtime_failures"] == [
        {
            "code": "FORBIDDEN",
            "category": "permission_required",
            "message": denied["message"],
            "retryable": False,
            "recovery_action": None,
        }
    ]


@pytest.mark.parametrize(
    "status",
    [["needs_data_model"], {"status": "needs_data_model"}, 42, [], {}, 0, False, None, " "],
)
@pytest.mark.asyncio
async def test_malformed_status_preserves_typed_failure_summary(status):
    final_text = json.dumps({"status": status, "data_requirements": [_topic_requirement()]})
    summary = _summarize_result([], final_text)
    assert summary["status"] == "error"
    assert "invalid status" in summary["message"]
    assert "data_requirements" not in summary

    published = _summarize_result(
        [
            ToolMessage(
                name="artifact_write",
                tool_call_id="write",
                content=json.dumps(
                    {
                        "status": "created",
                        "artifact": {"id": "saved", "version": 1},
                    }
                ),
            )
        ],
        final_text,
    )
    assert published["status"] == "created"
    assert published["artifact_id"] == "saved"
    assert "data_requirements" not in published

    failure = await _artifact_manager_failure_result(
        "parent", _SubagentTraceRecorder(), [], final_text, "The run failed."
    )
    assert failure["status"] == "error"
    assert failure["message"] == "The run failed."
    assert "data_requirements" not in failure
    assert any(
        event["type"] == "data-subagent-status" and event["data"]["phase"] == "failed"
        for event in failure["subagent_trace"]["events"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requirements", [[_topic_requirement()], [{}]])
async def test_failed_manager_run_never_returns_an_actionable_model_proposal(requirements):
    result = await _artifact_manager_failure_result(
        "parent",
        _SubagentTraceRecorder(),
        [],
        json.dumps(
            {
                "status": "needs_data_model",
                "data_requirements": requirements,
                "message": "A model change is needed.",
            }
        ),
        "The run failed.",
    )
    assert result["status"] == "error"
    assert "data_requirements" not in result
    assert "requirement_errors" not in result
    assert "subagent_message" not in result


@pytest.mark.parametrize("tool_status", ["checked", "error"])
@pytest.mark.parametrize("mixed_failure", [False, True])
def test_failed_check_preserves_model_gap_without_hiding_other_failures(tool_status, mixed_failure):
    failures = [{"category": "missing_model_dependency", "message": "visits.reviewed is missing"}]
    if mixed_failure:
        failures.append({"category": "permission_required", "message": "Access denied"})
    result = {
        "status": tool_status,
        "runtime": {"success": False, "failures": failures},
    }
    response = {
        "status": "needs_data_model",
        "message": "A reviewed dimension is missing; approval is required before creating it.",
        "data_requirements": [
            {
                "kind": "dimension",
                "source_datasets": ["visits"],
                "source_members": ["visits.status"],
                "grain": "One visit",
                "need": "Create visits.reviewed only after explicit approval.",
                "decisions": ["User must approve the classification rules"],
            }
        ],
    }
    messages = [
        ToolMessage(name="artifact_write", tool_call_id="check", content=json.dumps(result))
    ]
    summary = _summarize_result(messages, json.dumps(response))
    assert summary["status"] == ("error" if mixed_failure else "needs_data_model")
    assert summary["data_requirements"] == response["data_requirements"]
    assert summary["runtime_failures"] == failures
    if not mixed_failure:
        assert summary["message"] == response["message"]


@pytest.mark.parametrize("tool_status", ["checked", "error"])
@pytest.mark.parametrize("mixed_failure", [False, True])
def test_failed_check_preserves_correctable_proposal_only_for_pure_model_gap(
    tool_status, mixed_failure
):
    failures = [{"category": "missing_model_dependency", "message": "visits.reviewed is missing"}]
    if mixed_failure:
        failures.append({"category": "permission_required", "message": "Access denied"})
    messages = [
        ToolMessage(
            name="artifact_write",
            tool_call_id="check",
            content=json.dumps(
                {"status": tool_status, "runtime": {"success": False, "failures": failures}}
            ),
        )
    ]
    summary = _summarize_result(
        messages,
        json.dumps(
            {
                "status": "needs_data_model",
                "message": "A reviewed dimension is missing.",
                "data_requirements": [{}],
            }
        ),
    )

    assert summary["status"] == ("error" if mixed_failure else "invalid_data_requirements")
    assert "data_requirements" not in summary
    assert summary["runtime_failures"] == failures
    if mixed_failure:
        assert "requirement_errors" not in summary
        assert "subagent_message" not in summary
        assert "validation failed" in summary["message"]
    else:
        assert len(summary["requirement_errors"]) == 6
        assert summary["subagent_message"] == "A reviewed dimension is missing."
        assert "no model change is authorized" in summary["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content_blocks", [False, True], ids=["string", "provider_text_blocks"])
async def test_artifact_manager_tool_preserves_data_preparation_handoff(
    monkeypatch, content_blocks
):
    final = {
        "status": "needs_data_model",
        "message": "A reviewed topic field is needed.",
        "data_requirements": [_topic_requirement()],
    }

    class FakeGraph:
        async def astream_events(self, input_state, config, version):
            yield {
                "event": "on_chain_end",
                "data": {"output": {"messages": [_final_message(final, content_blocks)]}},
            }

    monkeypatch.setattr(
        "apps.agents.tools.artifact_manager_agent._build_artifact_manager_graph",
        lambda *args: FakeGraph(),
    )
    manager = create_artifact_manager_tool(SimpleNamespace(id="workspace"), None, [])

    result = await manager.ainvoke({"task": "Build an OCS topic dashboard."})

    assert result["status"] == "needs_data_model"
    assert result["data_requirements"] == final["data_requirements"]
    assert result["artifact_id"] is None


@pytest.mark.asyncio
async def test_nested_tool_output_is_truncated_with_marker():
    import asyncio

    queue: asyncio.Queue = asyncio.Queue()
    token = set_subagent_event_queue(queue)
    try:
        trace = _SubagentTraceRecorder()
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
            {},
            trace,
        )

        start = await queue.get()
        end = await queue.get()
    finally:
        reset_subagent_event_queue(token)

    assert start["event"]["type"] == "data-subagent-tool-input"
    assert end["event"]["type"] == "data-subagent-tool-output"
    assert "... (truncated, 100100 chars total)" in end["event"]["data"]["output"]


@pytest.mark.asyncio
async def test_nested_subagent_text_stream_is_persistable():
    import asyncio

    queue: asyncio.Queue = asyncio.Queue()
    token = set_subagent_event_queue(queue)
    try:
        trace = _SubagentTraceRecorder()
        message_buffers: dict[tuple[str, str], str] = {}
        for text in ("Building ", "artifact"):
            await _forward_nested_event(
                {
                    "event": "on_chat_model_stream",
                    "run_id": "run-model",
                    "data": {"chunk": AIMessage(content=text)},
                },
                "toolu_PARENT",
                {},
                {},
                message_buffers,
                trace,
            )

        first = await queue.get()
        second = await queue.get()
    finally:
        reset_subagent_event_queue(token)

    assert first["event"]["type"] == "data-subagent-text"
    assert first["event"]["data"]["text"] == "Building "
    assert second["event"]["data"]["text"] == "Building artifact"
    assert trace.to_dict()["events"] == [second["event"]]


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
    result = await tool.ainvoke(
        {
            "task": "create",
            "tool_call_id": "toolu_PARENT",
            "subagent_event_queue": queue,
        }
    )

    status = await queue.get()
    start = await queue.get()
    end = await queue.get()
    assert start["event"]["type"] == "data-subagent-tool-input"
    assert start["event"]["data"]["parentToolCallId"] == "toolu_PARENT"
    assert end["event"]["type"] == "data-subagent-tool-output"
    assert status["event"]["type"] == "data-subagent-status"
    assert result["subagent_trace"]["events"][0]["type"] == "data-subagent-status"
    assert any(
        event["type"] == "data-subagent-tool-output" for event in result["subagent_trace"]["events"]
    )


@pytest.mark.asyncio
async def test_artifact_manager_missing_task_returns_clean_validation_error():
    tool = create_artifact_manager_tool(
        SimpleNamespace(id="workspace-1"),
        SimpleNamespace(id="user-1"),
        [],
        conversation_id="thread-1",
    )

    result = await tool.ainvoke({"intent": "create"})

    assert result == ARTIFACT_MANAGER_TASK_REQUIRED_MESSAGE
    assert "ValidationError" not in result
    assert "subagent_event_queue" not in result


@pytest.mark.asyncio
async def test_artifact_manager_blank_task_returns_structured_failure(monkeypatch):
    def fail_if_graph_is_built(*args, **kwargs):
        raise AssertionError("missing task should fail before building the subagent graph")

    monkeypatch.setattr(
        "apps.agents.tools.artifact_manager_agent._build_artifact_manager_graph",
        fail_if_graph_is_built,
    )

    queue = __import__("asyncio").Queue()
    tool = create_artifact_manager_tool(
        SimpleNamespace(id="workspace-1"),
        SimpleNamespace(id="user-1"),
        [],
        conversation_id="thread-1",
    )
    result = await tool.ainvoke(
        {
            "task": "   ",
            "tool_call_id": "toolu_PARENT",
            "subagent_event_queue": queue,
        }
    )

    queued = []
    while not queue.empty():
        queued.append(await queue.get())

    assert result["status"] == "error"
    assert "non-empty task" in result["message"]
    assert result["message"] == ARTIFACT_MANAGER_TASK_REQUIRED_MESSAGE
    assert "subagent_event_queue" not in result["message"]
    assert "Field required" not in result["message"]
    assert [item["event"]["type"] for item in queued] == [
        "data-subagent-status",
        "data-subagent-error",
        "data-subagent-status",
    ]
    assert queued[-1]["event"]["data"]["phase"] == "failed"
    trace_events = result["subagent_trace"]["events"]
    assert any(
        event["type"] == "data-subagent-error" and "non-empty task" in event["data"]["message"]
        for event in trace_events
    )
    assert any(
        event["type"] == "data-subagent-status" and event["data"]["phase"] == "failed"
        for event in trace_events
    )


@pytest.mark.asyncio
async def test_artifact_manager_returns_failed_result_on_recursion_limit(monkeypatch):
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
            raise GraphRecursionError(
                "Recursion limit of 50 reached without hitting a stop condition"
            )

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
    result = await tool.ainvoke(
        {
            "task": "create",
            "tool_call_id": "toolu_PARENT",
            "subagent_event_queue": queue,
        }
    )

    queued = []
    while not queue.empty():
        queued.append(await queue.get())

    assert result["status"] == "error"
    assert result["artifact_id"] == "artifact-1"
    assert result["artifact_version"] == 1
    assert "Recursion limit of 50" in result["message"]
    assert any(
        event["type"] == "data-subagent-error" for event in result["subagent_trace"]["events"]
    )
    assert any(
        item["event"]["type"] == "data-subagent-status"
        and item["event"]["data"]["phase"] == "failed"
        for item in queued
    )
