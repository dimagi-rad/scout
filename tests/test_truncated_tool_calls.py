"""A tool call cut off by max_tokens is answered with an error, never run.

langchain parses partial tool JSON leniently, so the cut-off call arrives in
``tool_calls`` with plausible but truncated args.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from apps.agents.graph.base import MODEL_STOPPED_MESSAGES, build_agent_graph
from apps.agents.graph.state import (
    TRUNCATED_TOOL_CALL_MESSAGE,
    reject_truncated_tool_calls,
    truncated_retries_exhausted,
)
from apps.agents.tools.artifact_manager_agent import _build_artifact_manager_graph
from apps.agents.tools.canvas_manager_agent import _build_canvas_manager_graph


def _truncated(call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content=[
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": call_id, "name": "write", "input": {"sql": "SELECT"}},
        ],
        tool_calls=[{"id": call_id, "name": "write", "args": {"sql": "SELECT"}}],
        response_metadata={"stop_reason": "max_tokens"},
    )


TRUNCATED = _truncated()
ANSWER = AIMessage(content="Done.", response_metadata={"stop_reason": "end_turn"})


def _write_tool(calls: list):
    @tool
    def write(sql: str) -> str:
        """Write something."""
        calls.append(sql)
        return "written"

    return write


def _fake_llm(*responses):
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=list(responses))
    llm = MagicMock()
    llm.bind_tools.return_value = bound
    return llm


def _assert_rejected_then_answered(messages, calls):
    assert calls == []
    rejection = next(m for m in messages if isinstance(m, ToolMessage))
    assert rejection.tool_call_id == "call-1"
    assert rejection.status == "error"
    assert rejection.content == TRUNCATED_TOOL_CALL_MESSAGE
    assert messages[-1].content == "Done."


@pytest.mark.asyncio
async def test_main_agent_does_not_run_truncated_tool_call():
    calls: list = []
    with (
        patch("apps.agents.graph.base.ChatAnthropic", return_value=_fake_llm(TRUNCATED, ANSWER)),
        patch("apps.agents.graph.base._build_tools", return_value=[_write_tool(calls)]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            new=AsyncMock(return_value=("prompt", "")),
        ),
    ):
        graph = await build_agent_graph(
            SimpleNamespace(id="ws-1"), MagicMock(is_authenticated=False)
        )
        result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    _assert_rejected_then_answered(result["messages"], calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prefix", "tools_factory", "builder"),
    [
        (
            "apps.agents.tools.canvas_manager_agent",
            "create_canvas_tools",
            _build_canvas_manager_graph,
        ),
        (
            "apps.agents.tools.artifact_manager_agent",
            "create_artifact_graph_tools",
            _build_artifact_manager_graph,
        ),
    ],
)
async def test_subagent_does_not_run_truncated_tool_call(prefix, tools_factory, builder):
    calls: list = []
    with (
        patch("apps.agents.graph.nested.ChatAnthropic", return_value=_fake_llm(TRUNCATED, ANSWER)),
        patch(f"{prefix}.{tools_factory}", return_value=[_write_tool(calls)]),
    ):
        graph = builder(SimpleNamespace(id="ws-1"), None, [], None)
        result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    _assert_rejected_then_answered(result["messages"], calls)


SUBAGENTS = [
    ("apps.agents.tools.canvas_manager_agent", "create_canvas_tools", _build_canvas_manager_graph),
    (
        "apps.agents.tools.artifact_manager_agent",
        "create_artifact_graph_tools",
        _build_artifact_manager_graph,
    ),
]


@pytest.mark.asyncio
async def test_main_agent_gives_up_after_repeated_truncation():
    """A request that keeps overrunning max_tokens ends with the fallback message,
    not a run of 16k-token retries up to the recursion limit."""
    calls: list = []
    llm = _fake_llm(_truncated("call-1"), _truncated("call-2"), ANSWER)
    with (
        patch("apps.agents.graph.base.ChatAnthropic", return_value=llm),
        patch("apps.agents.graph.base._build_tools", return_value=[_write_tool(calls)]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            new=AsyncMock(return_value=("prompt", "")),
        ),
    ):
        graph = await build_agent_graph(
            SimpleNamespace(id="ws-1"), MagicMock(is_authenticated=False)
        )
        result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})

    messages = result["messages"]
    assert calls == []
    assert llm.bind_tools.return_value.ainvoke.await_count == 2
    assert isinstance(messages[-2], ToolMessage)
    assert messages[-1].content == MODEL_STOPPED_MESSAGES["max_tokens"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("prefix", "tools_factory", "builder"), SUBAGENTS)
async def test_subagent_gives_up_after_repeated_truncation(prefix, tools_factory, builder):
    calls: list = []
    llm = _fake_llm(_truncated("call-1"), _truncated("call-2"), ANSWER)
    with (
        patch("apps.agents.graph.nested.ChatAnthropic", return_value=llm),
        patch(f"{prefix}.{tools_factory}", return_value=[_write_tool(calls)]),
    ):
        graph = builder(SimpleNamespace(id="ws-1"), None, [], None)
        result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})

    assert calls == []
    assert llm.bind_tools.return_value.ainvoke.await_count == 2
    last = result["messages"][-1]
    assert isinstance(last, ToolMessage)
    assert last.content == TRUNCATED_TOOL_CALL_MESSAGE


def _ok_turn(call_id: str) -> list:
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": "write", "args": {}}]),
        ToolMessage(content="written", tool_call_id=call_id, name="write"),
    ]


def _rejected_turn(call_id: str) -> list:
    truncated = _truncated(call_id)
    return [truncated, *reject_truncated_tool_calls({"messages": [truncated]})["messages"]]


def test_only_consecutive_truncations_exhaust_the_retries():
    """A run that recovered from one truncation isn't stopped by a later, unrelated one."""
    recovered = [
        HumanMessage(content="hi"),
        *_rejected_turn("t-1"),
        *_ok_turn("ok-1"),
        _truncated("t-2"),
    ]
    assert not truncated_retries_exhausted(recovered)

    back_to_back = [HumanMessage(content="hi"), *_rejected_turn("t-1"), _truncated("t-2")]
    assert truncated_retries_exhausted(back_to_back)

    earlier_turn = [*_rejected_turn("t-0"), HumanMessage(content="again"), _truncated("t-1")]
    assert not truncated_retries_exhausted(earlier_turn)
