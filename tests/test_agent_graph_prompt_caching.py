"""Tests for Anthropic prompt caching + history/knowledge budgets (arch #254).

Covers findings:

- ``02#3`` — the agent attaches an Anthropic ``cache_control`` breakpoint to the
  stable system prefix (tools + frozen system render together and cache) and to
  the most-recent conversation turn, so the large static prefix and the replayed
  history are billed at cache-read rates rather than full input on every call.
- ``01#3`` — conversation history is bounded (``prune_messages`` is actually
  wired into the agent node) so per-turn input tokens don't grow without limit.
- ``02#3`` (prefix-stability half) — volatile row-counts / timestamps are NOT in
  the cached system prefix; they live after the breakpoint, so a new
  materialization doesn't invalidate the cached prefix.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from apps.agents.graph import base as graph_base
from apps.agents.graph.base import PROMPT_CACHE_CONTROL, _build_cached_system_message
from apps.agents.graph.state import DEFAULT_MAX_MESSAGES, prune_messages


def _cache_breakpoint_blocks(content) -> list[dict]:
    """Return content blocks carrying a cache_control breakpoint."""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("cache_control")]


@pytest.mark.asyncio
async def test_system_prompt_split_into_stable_and_volatile(monkeypatch):
    """_build_system_prompt returns a (stable, volatile) split.

    The stable section holds the frozen base prompt + knowledge; the volatile
    section holds tenant context / schema availability (row counts, timestamps).
    """
    graph_base._system_prompt_cache.clear()
    workspace = MagicMock()
    workspace.id = "ws-split"
    workspace.system_prompt = "WS instructions"
    workspace.tenants = MagicMock()
    workspace.tenants.acount = AsyncMock(return_value=0)
    user = MagicMock()
    user.id = "u1"

    with patch("apps.agents.graph.base.KnowledgeRetriever") as MockRetriever:
        mock_retriever = MagicMock()
        mock_retriever.retrieve = AsyncMock(return_value="## Knowledge Base\n\nMetric X")
        MockRetriever.return_value = mock_retriever

        stable, volatile = await graph_base._build_system_prompt(workspace, user)

    assert isinstance(stable, str)
    assert isinstance(volatile, str)
    # The base prompt + workspace instructions + knowledge are stable.
    assert "WS instructions" in stable
    assert "Metric X" in stable


@pytest.mark.asyncio
async def test_volatile_schema_not_in_stable_prefix(monkeypatch):
    """Row counts / last-materialized timestamps must live in the volatile section.

    Caching keys off exact prefix bytes; a per-materialization row count in the
    cached prefix would defeat every cache hit (02#3 prefix-stability half).
    """
    graph_base._system_prompt_cache.clear()
    workspace = MagicMock()
    workspace.id = "ws-vol"
    workspace.system_prompt = ""
    workspace.tenants = MagicMock()
    workspace.tenants.acount = AsyncMock(return_value=1)
    tenant = MagicMock()
    tenant.canonical_name = "Acme"
    tenant.external_id = "acme"
    tenant.get_provider_display = MagicMock(return_value="CommCare")
    tenant.provider = "commcare"
    workspace.tenants.afirst = AsyncMock(return_value=tenant)
    user = MagicMock()
    user.id = "u2"

    schema_block = "Data is loaded. Last updated: 2026-06-25. cases (1,234 rows)"

    with (
        patch("apps.agents.graph.base.KnowledgeRetriever") as MockRetriever,
        patch(
            "apps.agents.graph.base._fetch_schema_context",
            new=AsyncMock(return_value=schema_block),
        ),
        patch("apps.agents.graph.base.get_registry") as mock_reg,
    ):
        MockRetriever.return_value = MagicMock(retrieve=AsyncMock(return_value=""))
        mock_reg.return_value.get_by_provider.return_value = MagicMock(name="pc")

        stable, volatile = await graph_base._build_system_prompt(workspace, user)

    # The volatile schema text (row counts + timestamp) is NOT in the cached prefix.
    assert "1,234 rows" not in stable
    assert "2026-06-25" not in stable
    assert "1,234 rows" in volatile


@pytest.mark.asyncio
async def test_agent_node_applies_cache_control_breakpoints():
    """agent_node builds a list-content SystemMessage with a cache breakpoint and
    passes cache_control through to ainvoke for the conversation history.
    """
    workspace = MagicMock()
    workspace.id = "ws-cc"
    user = MagicMock()
    user.id = "u3"

    captured = {}

    async def fake_ainvoke(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return AIMessage(content="ok")

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = MagicMock(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    with (
        patch("apps.agents.graph.base.ChatAnthropic", return_value=mock_llm),
        patch("apps.agents.graph.base._build_tools", return_value=[]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            new=AsyncMock(return_value=("STABLE PREFIX", "VOLATILE SUFFIX")),
        ),
    ):
        graph = await graph_base.build_agent_graph(workspace, user)

    # Drive a single agent turn through the compiled graph.
    state = {
        "messages": [HumanMessage(content="hi")],
        "workspace_id": "ws-cc",
        "user_id": "u3",
        "user_role": "analyst",
        "thread_id": "t1",
    }
    await graph.ainvoke(state, {"recursion_limit": 5})

    # The history cache breakpoint: cache_control kwarg passed to ainvoke.
    assert captured["kwargs"].get("cache_control") == {"type": "ephemeral"}

    # The system message is a list of content blocks; the last stable block
    # carries a cache_control breakpoint (caches tools + system together).
    sys_msgs = [m for m in captured["messages"] if isinstance(m, SystemMessage)]
    assert sys_msgs, "expected a SystemMessage in the invoked messages"
    sys_content = sys_msgs[0].content
    assert isinstance(sys_content, list), "system content must be list-of-blocks to cache"
    bp = _cache_breakpoint_blocks(sys_content)
    assert len(bp) == 1, "exactly one cache breakpoint on the stable system block"
    assert "STABLE PREFIX" in bp[0]["text"]
    # The volatile suffix must come AFTER the breakpoint (uncached, last block).
    assert any("VOLATILE SUFFIX" in b.get("text", "") for b in sys_content)
    last_block = sys_content[-1]
    assert "VOLATILE SUFFIX" in last_block.get("text", "")
    assert not last_block.get("cache_control")


@pytest.mark.asyncio
async def test_agent_node_prunes_history():
    """agent_node bounds replayed history via prune_messages (01#3)."""
    workspace = MagicMock()
    workspace.id = "ws-prune"
    user = MagicMock()

    captured = {}

    async def fake_ainvoke(messages, **kwargs):
        captured["messages"] = messages
        return AIMessage(content="done")

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = MagicMock(ainvoke=AsyncMock(side_effect=fake_ainvoke))

    with (
        patch("apps.agents.graph.base.ChatAnthropic", return_value=mock_llm),
        patch("apps.agents.graph.base._build_tools", return_value=[]),
        patch(
            "apps.agents.graph.base._build_system_prompt",
            new=AsyncMock(return_value=("S", "V")),
        ),
    ):
        graph = await graph_base.build_agent_graph(workspace, user)

    # Build a long history (well past DEFAULT_MAX_MESSAGES) of alternating turns.
    history = []
    for i in range(DEFAULT_MAX_MESSAGES + 30):
        history.append(HumanMessage(content=f"q{i}"))
        history.append(AIMessage(content=f"a{i}"))
    state = {
        "messages": history,
        "workspace_id": "ws-prune",
        "user_id": "u",
        "user_role": "analyst",
        "thread_id": "t",
    }
    await graph.ainvoke(state, {"recursion_limit": 5})

    # The messages handed to the LLM (excluding the system message) are bounded.
    non_system = [m for m in captured["messages"] if not isinstance(m, SystemMessage)]
    assert len(non_system) <= DEFAULT_MAX_MESSAGES


def test_anthropic_request_payload_carries_cache_breakpoints():
    """The wire request langchain-anthropic builds carries cache_control on the
    system prefix and the last message block.

    The real API reports a non-zero ``usage.cache_read_input_tokens`` only when
    the request payload presents these ``cache_control`` breakpoints with a
    stable prefix. We can't hit the live API in CI, so we assert on the exact
    payload ``ChatAnthropic`` would send (its ``_get_request_payload``) — the
    deterministic, offline equivalent of verifying cache hits (arch #254, 02#3).
    """
    llm = ChatAnthropic(model="claude-opus-4-8", max_tokens=64, api_key="test-key")

    system_msg = _build_cached_system_message("STABLE PREFIX " * 50, "VOLATILE SUFFIX")
    messages = [system_msg, HumanMessage(content="hello")]

    payload = llm._get_request_payload(messages, cache_control=PROMPT_CACHE_CONTROL)

    # System block (renders after tools) carries the breakpoint -> caches
    # tools + system together.
    system = payload["system"]
    assert isinstance(system, list)
    stable_blocks = [b for b in system if b.get("cache_control")]
    assert len(stable_blocks) == 1
    assert "STABLE PREFIX" in stable_blocks[0]["text"]
    # The volatile block has NO cache_control and comes last in system.
    assert system[-1].get("cache_control") is None
    assert "VOLATILE SUFFIX" in system[-1]["text"]

    # The conversation-history breakpoint landed on the last message block.
    last_msg = payload["messages"][-1]
    content = last_msg["content"]
    assert isinstance(content, list), "expected list content with a cache_control block"
    assert content[-1].get("cache_control") == PROMPT_CACHE_CONTROL


def test_prune_messages_keeps_tool_pairs():
    """prune_messages never strands a ToolMessage from its AIMessage (01#3)."""
    msgs = [HumanMessage(content="q")]
    for i in range(DEFAULT_MAX_MESSAGES):
        ai = AIMessage(content="", tool_calls=[{"name": "query", "args": {}, "id": f"tc{i}"}])
        tool = ToolMessage(content="rows", tool_call_id=f"tc{i}", name="query")
        msgs.extend([ai, tool])
    pruned = prune_messages(msgs, max_messages=5)
    # Must not begin with an orphaned ToolMessage.
    assert not isinstance(pruned[0], ToolMessage)
