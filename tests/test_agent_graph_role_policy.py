"""Graph tools and prompts reflect the actor's workspace capabilities."""

from unittest.mock import AsyncMock

import pytest

from apps.agents.graph.base import _build_system_prompt, _build_tools, _system_prompt_cache


def _by_name(tools):
    return {item.name: item for item in tools}


@pytest.mark.django_db
def test_read_graph_keeps_queries_and_artifact_inspection_but_filters_writes(workspace, read_user):
    query = AsyncMock()
    query.name = "query"
    run_materialization = AsyncMock()
    run_materialization.name = "run_materialization"

    tools = _by_name(
        _build_tools(
            workspace,
            read_user,
            [query, run_materialization],
            conversation_id="thread",
            interactive=True,
            write_capable=False,
        )
    )

    assert "query" in tools
    assert "artifact_graph_overview" in tools
    assert "get_artifact_semantic_queries" in tools
    assert "canvas_read" in tools
    assert {
        "run_materialization",
        "save_learning",
        "save_as_recipe",
        "artifact_manager",
        "artifact_write",
        "canvas_manager",
    }.isdisjoint(tools)


@pytest.mark.django_db
def test_write_graph_advertises_shared_mutations(workspace, write_user):
    run_materialization = AsyncMock()
    run_materialization.name = "run_materialization"

    tools = _by_name(
        _build_tools(
            workspace,
            write_user,
            [run_materialization],
            conversation_id="thread",
            interactive=True,
            canvas_write=True,
            write_capable=True,
        )
    )

    assert {
        "run_materialization",
        "save_learning",
        "save_as_recipe",
        "artifact_manager",
        "canvas_manager",
    } <= tools.keys()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_read_prompt_matches_filtered_write_capabilities(workspace, read_user):
    _system_prompt_cache.clear()
    stable, volatile = await _build_system_prompt(
        workspace,
        read_user,
        interactive=False,
        canvas_write=False,
        write_capable=False,
    )
    prompt = stable + volatile

    assert "workspace role is read-only" in prompt
    assert "`artifact_graph_overview`" in prompt
    assert "Call `run_materialization`" not in prompt
    assert "read-write workspace role" in prompt
