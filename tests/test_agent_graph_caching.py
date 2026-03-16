"""Tests for agent graph and system prompt caching."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_system_prompt_is_cached_across_calls():
    """Calling _build_system_prompt twice with same workspace returns cached result."""
    from apps.agents.graph.base import _build_system_prompt, _system_prompt_cache

    _system_prompt_cache.clear()

    workspace = MagicMock()
    workspace.id = "test-ws-id"
    workspace.system_prompt = "Test instructions"
    workspace.tenants = MagicMock()
    workspace.tenants.acount = AsyncMock(return_value=0)

    user = MagicMock()
    user.id = "test-user-id"

    with patch("apps.agents.graph.base.KnowledgeRetriever") as MockRetriever:
        mock_retriever = MagicMock()
        mock_retriever.retrieve = AsyncMock(return_value="knowledge context")
        MockRetriever.return_value = mock_retriever

        result1 = await _build_system_prompt(workspace, user)
        result2 = await _build_system_prompt(workspace, user)

        assert result1 == result2
        assert MockRetriever.call_count == 1


@pytest.mark.asyncio
async def test_system_prompt_cache_invalidates_on_prompt_change():
    """Cache miss when workspace system_prompt changes."""
    from apps.agents.graph.base import _build_system_prompt, _system_prompt_cache

    _system_prompt_cache.clear()

    workspace = MagicMock()
    workspace.id = "test-ws-id-2"
    workspace.system_prompt = "Instructions v1"
    workspace.tenants = MagicMock()
    workspace.tenants.acount = AsyncMock(return_value=0)

    user = MagicMock()

    with patch("apps.agents.graph.base.KnowledgeRetriever") as MockRetriever:
        mock_retriever = MagicMock()
        mock_retriever.retrieve = AsyncMock(return_value="")
        MockRetriever.return_value = mock_retriever

        result1 = await _build_system_prompt(workspace, user)

        workspace.system_prompt = "Instructions v2"
        result2 = await _build_system_prompt(workspace, user)

        assert result1 != result2
        assert MockRetriever.call_count == 2
