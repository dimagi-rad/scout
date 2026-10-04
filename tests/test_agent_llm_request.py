"""The wire request every agent LLM sends: thinking, betas, effort and timeout.

Each site's ChatAnthropic kwargs are captured, then fed to a real ChatAnthropic
so the assertions run against the payload langchain-anthropic would send.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from django.conf import settings
from django.test import override_settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from apps.agents.graph.base import build_agent_graph
from apps.agents.llm_request import THINKING_BINDING_BETA, chat_model_kwargs
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
        patch("apps.agents.graph.nested.ChatAnthropic") as chat,
        patch(f"{prefix}.create_canvas_tools", return_value=[]),
    ):
        _build_canvas_manager_graph(SimpleNamespace(id="ws-1"), None, [], None)
    return chat.call_args.kwargs


def _artifact_kwargs() -> dict:
    prefix = "apps.agents.tools.artifact_manager_agent"
    with (
        patch("apps.agents.graph.nested.ChatAnthropic") as chat,
        patch(f"{prefix}.create_artifact_graph_tools", return_value=[]),
    ):
        _build_artifact_manager_graph(SimpleNamespace(id="ws-1"), None, [], None)
    return chat.call_args.kwargs


@pytest.fixture(params=["main", "canvas", "artifact"])
def site(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def site_kwargs(site) -> dict:
    if site == "main":
        return await _main_agent_kwargs()
    if site == "canvas":
        return _canvas_kwargs()
    return _artifact_kwargs()


@pytest.fixture
def site_payload(site_kwargs) -> dict:
    return _payload(site_kwargs)


@pytest.mark.asyncio
async def test_stale_thinking_blocks_are_dropped_not_rejected(site_payload):
    """A pruned history or a new date line must never 400 a thread."""
    thinking = site_payload["thinking"]
    assert thinking["type"] == "adaptive"
    assert thinking["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert THINKING_BINDING_BETA in site_payload["betas"]


@pytest.mark.asyncio
async def test_thinking_is_summarized_so_the_thinking_card_has_text(site_payload):
    """The default display ("omitted") streams empty thinking text, and the
    Thinking card (and Opus 5.5's between-tool-call notes) render nothing."""
    assert site_payload["thinking"]["display"] == "summarized"


@pytest.mark.asyncio
async def test_effort_is_explicit_per_site(site, site_payload):
    """Opus 5.5 defaults to medium; pin it so a model change can't shift it
    silently, and run the narrow subagents at low."""
    expected = {"main": "medium", "canvas": "low", "artifact": "low"}[site]
    assert site_payload["output_config"]["effort"] == expected


@pytest.mark.asyncio
async def test_every_site_bounds_each_request(site_kwargs):
    """langchain-anthropic defaults to timeout=None, which disables the SDK's own
    cap: one hung connection would then stall the turn with no end."""
    llm = ChatAnthropic(**site_kwargs, api_key="test-key")
    assert llm._async_client.timeout == settings.LLM_REQUEST_TIMEOUT_S == 120.0


@override_settings(LLM_REQUEST_TIMEOUT_S=42.0)
def test_request_timeout_follows_the_setting():
    assert chat_model_kwargs("low")["timeout"] == 42.0
