"""A turn the model ends without an answer gets a short fallback message.

A refusal, a max_tokens cut-off, or a thinking-only turn would otherwise end
the chat with a blank reply and a clean finish.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from apps.agents.graph.base import (
    ESCALATION_METADATA_KEY,
    MODEL_STOPPED_MESSAGES,
    build_agent_graph,
)
from apps.chat.stream import langgraph_to_ui_stream

THINKING = {"type": "thinking", "thinking": "", "signature": "sig"}


async def _run_turn(response: AIMessage) -> list:
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    llm = MagicMock()
    llm.bind_tools.return_value = bound
    with (
        patch("apps.agents.graph.base.ChatAnthropic", return_value=llm),
        patch("apps.agents.graph.base._build_tools", return_value=[]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            new=AsyncMock(return_value=("prompt", "")),
        ),
    ):
        graph = await build_agent_graph(
            SimpleNamespace(id="ws-1"), MagicMock(is_authenticated=False)
        )
        result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})
    return result["messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            AIMessage(content=[THINKING], response_metadata={"stop_reason": "refusal"}),
            "refusal",
        ),
        (
            AIMessage(
                content=[THINKING, {"type": "text", "text": "The top three are"}],
                response_metadata={"stop_reason": "max_tokens"},
            ),
            "max_tokens",
        ),
        (
            AIMessage(content=[THINKING], response_metadata={"stop_reason": "end_turn"}),
            "empty",
        ),
    ],
)
async def test_unanswered_turn_ends_with_fallback_message(response, reason):
    messages = await _run_turn(response)

    last = messages[-1]
    assert last.content == MODEL_STOPPED_MESSAGES[reason]
    assert last.response_metadata[ESCALATION_METADATA_KEY] == f"model_{reason}"
    # The model's own turn stays in history unchanged, thinking block included.
    assert messages[-2].content == response.content


@pytest.mark.asyncio
async def test_answered_turn_gets_no_fallback():
    answer = AIMessage(
        content=[THINKING, {"type": "text", "text": "42 orders."}],
        response_metadata={"stop_reason": "end_turn"},
    )
    messages = await _run_turn(answer)
    assert messages[-1].content == answer.content


@pytest.mark.asyncio
async def test_fallback_message_streams_to_the_chat():
    agent = MagicMock()

    async def fake_events(*args, **kwargs):
        yield {
            "event": "on_chain_end",
            "name": "model_stopped",
            "data": {
                "output": {"messages": [AIMessage(content=MODEL_STOPPED_MESSAGES["refusal"])]}
            },
        }

    agent.astream_events = fake_events

    events = []
    async for sse in langgraph_to_ui_stream(agent, {}, {}):
        for line in sse.strip().split("\n"):
            if line.strip().startswith("data: "):
                events.append(json.loads(line.strip()[6:]))

    text = [e["delta"] for e in events if e["type"] == "text-delta"]
    assert text == [MODEL_STOPPED_MESSAGES["refusal"]]
