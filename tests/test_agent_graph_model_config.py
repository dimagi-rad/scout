"""The agent graph's LLM model is driven by settings.DEFAULT_LLM_MODEL.

The setting previously existed but was unused while the model string was
hardcoded in ``build_agent_graph``. These tests guard against that drift
recurring and confirm the env-var override path works.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.conf import settings
from django.test import override_settings

from apps.agents.graph.base import build_agent_graph


@pytest.mark.asyncio
async def test_llm_model_comes_from_settings():
    """ChatAnthropic is built with settings.DEFAULT_LLM_MODEL, not a hardcoded id."""
    workspace = MagicMock()
    workspace.id = "ws-1"
    user = MagicMock()

    with (
        override_settings(DEFAULT_LLM_MODEL="sentinel-model-id"),
        patch("apps.agents.graph.base.ChatAnthropic") as MockChat,
        patch("apps.agents.graph.base._build_tools", return_value=[]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            # Returns a (stable, volatile) split since arch #254 (02#3).
            new=AsyncMock(return_value=("prompt", "")),
        ),
    ):
        await build_agent_graph(workspace, user)

    assert MockChat.call_count == 1
    assert MockChat.call_args.kwargs["model"] == "sentinel-model-id"


def test_default_llm_model_is_opus_4_8():
    """With DEFAULT_LLM_MODEL unset, the default resolves to claude-opus-4-8."""
    assert settings.DEFAULT_LLM_MODEL == "claude-opus-4-8"
