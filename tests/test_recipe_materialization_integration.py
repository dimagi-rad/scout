"""End-to-end: a recipe whose agent calls run_materialization runs headlessly
through the REAL agent graph (interactive=False) without the synthetic-thread_id
crash, using the blocking materialize tool instead of the chat fire-and-ack one.

This is the capstone for the recipe-materialization fix: it exercises the real
wiring (RecipeRunner -> build_agent_graph(interactive=False) -> _build_tools tool
swap -> injecting node -> blocking run_materialization -> materialize_workspace_
core), with only the LLM and the pipeline core stubbed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from apps.chat.models import Thread, ThreadJob
from apps.recipes.models import Recipe, RecipeRun, RecipeRunStatus
from apps.recipes.services.runner import RecipeRunner


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_recipe_agent_materializes_headlessly_without_crash(workspace, user, monkeypatch):
    recipe = await Recipe.objects.acreate(
        workspace=workspace,
        name="Refresh & Build",
        description="",
        prompt="Refresh the data, then build a dashboard.",
        variables=[],
        created_by=user,
    )

    # Stub the pipeline core so no real materialization runs, and record that the
    # BLOCKING path was taken (proves the headless tool ran, not the MCP one).
    core_calls = {"n": 0, "job_id": None}

    async def fake_core(workspace_id, user_id="", job_id=None):
        core_calls["n"] += 1
        core_calls["job_id"] = job_id
        return {
            "all_succeeded": True,
            "tenants": [{"tenant": "t1", "success": True}],
            "view_schema": None,
        }

    monkeypatch.setattr("apps.workspaces.tasks.materialize_workspace_core", fake_core)

    # Stub the LLM: turn 1 calls run_materialization; turn 2 finishes the run.
    llm_calls = {"n": 0}

    async def fake_ainvoke(messages, *args, **kwargs):
        llm_calls["n"] += 1
        if llm_calls["n"] == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "run_materialization", "args": {}, "id": "call_mat"}],
            )
        return AIMessage(content="Data refreshed and dashboard built.", id="ai-final")

    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(side_effect=fake_ainvoke)
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_bound

    with (
        patch("apps.recipes.services.runner.get_mcp_tools", new=AsyncMock(return_value=[])),
        patch("apps.recipes.services.runner.get_user_oauth_tokens", new=AsyncMock(return_value={})),
        patch("apps.agents.graph.base.ChatAnthropic", return_value=mock_llm),
    ):
        run_row = await RecipeRun.objects.acreate(
            recipe=recipe,
            run_by=user,
            status=RecipeRunStatus.PENDING,
            variable_values={},
            step_results=[],
        )
        run = await RecipeRunner(recipe, {}, user, run=run_row, job_id=55).execute_async()

    # No UUID crash — the run completed.
    assert run.status == RecipeRunStatus.COMPLETED, run.step_results
    # The blocking materialize actually executed (and got the run's job_id).
    assert core_calls["n"] == 1
    assert core_calls["job_id"] == 55
    assert "run_materialization" in run.step_results[0]["tools_used"]
    # Headless: no chat Thread/ThreadJob created anywhere in the flow.
    assert not await Thread.objects.aexists()
    assert not await ThreadJob.objects.aexists()
