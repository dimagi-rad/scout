"""The wire request every agent LLM sends: thinking, betas and effort.

Each site's ChatAnthropic kwargs are captured, then fed to a real ChatAnthropic
so the assertions run against the payload langchain-anthropic would send.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from apps.agents.graph.base import build_agent_graph
from apps.agents.llm_request import THINKING_BINDING_BETA
from apps.agents.tools.artifact_manager_agent import _build_artifact_manager_graph
from apps.agents.tools.canvas_manager_agent import _build_canvas_manager_graph


def _payload(kwargs: dict) -> dict:
    llm = ChatAnthropic(**kwargs, api_key="test-key")
    return llm._get_request_payload([HumanMessage(content="hi")])


async def _main_agent_kwargs() -> dict:
    with (
        patch("apps.agents.graph.base.ChatAnthropic") as chat,
        patch("apps.agents.graph.base._build_tools", return_value=[]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            new=AsyncMock(return_value=("prompt", "")),
        ),
    ):
        await build_agent_graph(SimpleNamespace(id="ws-1"), MagicMock(is_authenticated=False))
    return chat.call_args.kwargs


def _canvas_kwargs() -> dict:
    prefix = "apps.agents.tools.canvas_manager_agent"
    with (
        patch(f"{prefix}.ChatAnthropic") as chat,
        patch(f"{prefix}.create_canvas_tools", return_value=[]),
    ):
        _build_canvas_manager_graph(SimpleNamespace(id="ws-1"), None, [], None)
    return chat.call_args.kwargs


def _artifact_kwargs() -> dict:
    prefix = "apps.agents.tools.artifact_manager_agent"
    with (
        patch(f"{prefix}.ChatAnthropic") as chat,
        patch(f"{prefix}.create_artifact_graph_tools", return_value=[]),
    ):
        _build_artifact_manager_graph(SimpleNamespace(id="ws-1"), None, [], None)
    return chat.call_args.kwargs


@pytest_asyncio.fixture(params=["main", "canvas", "artifact"])
async def site_payload(request) -> dict:
    if request.param == "main":
        kwargs = await _main_agent_kwargs()
    elif request.param == "canvas":
        kwargs = _canvas_kwargs()
    else:
        kwargs = _artifact_kwargs()
    return _payload(kwargs)


@pytest.mark.asyncio
async def test_stale_thinking_blocks_are_dropped_not_rejected(site_payload):
    """A pruned history or a new date line must never 400 a thread."""
    thinking = site_payload["thinking"]
    assert thinking["type"] == "adaptive"
    assert thinking["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert THINKING_BINDING_BETA in site_payload["betas"]
