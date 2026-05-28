"""Unit tests for the graph-level panic-loop circuit breaker.

These exercise ``_should_escalate`` directly — the helper that decides
whether the post-tools router should hand off to the terminal escalation
node. They use bare ``ToolMessage`` / ``AIMessage`` objects (no DB, no LLM)
so they're cheap and deterministic.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apps.agents.graph.base import _should_escalate


def _err_tool_message(code: str, tool_call_id: str = "tc", name: str = "query") -> ToolMessage:
    """Build a ToolMessage with a JSON-encoded error envelope, matching the
    shape ``mcp_server.envelope.error_response`` produces.
    """
    body = json.dumps({"success": False, "error": {"code": code, "message": f"simulated {code}"}})
    return ToolMessage(content=body, tool_call_id=tool_call_id, name=name)


def _ok_tool_message(tool_call_id: str = "tc", name: str = "query") -> ToolMessage:
    body = json.dumps({"success": True, "data": {"rows": [[1]]}})
    return ToolMessage(content=body, tool_call_id=tool_call_id, name=name)


def _ai_with_tool_call(tool_call_id: str = "tc", name: str = "query") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": tool_call_id, "name": name, "args": {}}],
    )


class TestShouldEscalate:
    def test_three_consecutive_not_found_triggers_escalation(self):
        messages = [
            HumanMessage(content="how many users?"),
            _ai_with_tool_call("a"),
            _err_tool_message("NOT_FOUND", "a"),
            _ai_with_tool_call("b"),
            _err_tool_message("NOT_FOUND", "b"),
            _ai_with_tool_call("c"),
            _err_tool_message("NOT_FOUND", "c"),
        ]
        assert _should_escalate(messages) is True

    def test_three_consecutive_validation_error_triggers_escalation(self):
        messages = [
            HumanMessage(content="how many users?"),
            _ai_with_tool_call("a"),
            _err_tool_message("VALIDATION_ERROR", "a"),
            _ai_with_tool_call("b"),
            _err_tool_message("VALIDATION_ERROR", "b"),
            _ai_with_tool_call("c"),
            _err_tool_message("VALIDATION_ERROR", "c"),
        ]
        assert _should_escalate(messages) is True

    def test_three_mixed_errors_trigger_escalation(self):
        # NOT_FOUND, VALIDATION_ERROR, NOT_FOUND in sequence — the rule is
        # "all errors", not "all the same error code".
        messages = [
            HumanMessage(content="how many users?"),
            _ai_with_tool_call("a"),
            _err_tool_message("NOT_FOUND", "a"),
            _ai_with_tool_call("b"),
            _err_tool_message("VALIDATION_ERROR", "b"),
            _ai_with_tool_call("c"),
            _err_tool_message("NOT_FOUND", "c"),
        ]
        assert _should_escalate(messages) is True

    def test_single_not_found_does_not_trigger_escalation(self):
        # One transient error is normal. The agent should still get a turn
        # to self-correct.
        messages = [
            HumanMessage(content="how many users?"),
            _ai_with_tool_call("a"),
            _err_tool_message("NOT_FOUND", "a"),
        ]
        assert _should_escalate(messages) is False

    def test_two_errors_does_not_trigger_escalation(self):
        # Threshold is 3 — two errors is "trying twice" which the prompt
        # explicitly allows.
        messages = [
            HumanMessage(content="how many users?"),
            _ai_with_tool_call("a"),
            _err_tool_message("NOT_FOUND", "a"),
            _ai_with_tool_call("b"),
            _err_tool_message("NOT_FOUND", "b"),
        ]
        assert _should_escalate(messages) is False

    def test_success_between_errors_resets_streak(self):
        # If the agent successfully recovered at some point, the streak is
        # broken — we only look at the trailing run of tool messages.
        messages = [
            HumanMessage(content="how many users?"),
            _ai_with_tool_call("a"),
            _err_tool_message("NOT_FOUND", "a"),
            _ai_with_tool_call("b"),
            _err_tool_message("NOT_FOUND", "b"),
            _ai_with_tool_call("c"),
            _ok_tool_message("c"),
        ]
        assert _should_escalate(messages) is False

    def test_other_error_codes_do_not_trigger_escalation(self):
        # The breaker is scoped to schema-drift signals. A query timeout
        # or auth error is not a panic-loop pattern.
        messages = [
            HumanMessage(content="how many users?"),
            _ai_with_tool_call("a"),
            ToolMessage(
                content='{"success": false, "error": {"code": "QUERY_TIMEOUT"}}',
                tool_call_id="a",
                name="query",
            ),
            _ai_with_tool_call("b"),
            ToolMessage(
                content='{"success": false, "error": {"code": "QUERY_TIMEOUT"}}',
                tool_call_id="b",
                name="query",
            ),
            _ai_with_tool_call("c"),
            ToolMessage(
                content='{"success": false, "error": {"code": "QUERY_TIMEOUT"}}',
                tool_call_id="c",
                name="query",
            ),
        ]
        assert _should_escalate(messages) is False

    def test_empty_message_list_does_not_trigger_escalation(self):
        assert _should_escalate([]) is False
