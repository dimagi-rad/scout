"""A subagent run the model refused or cut off is reported to the parent as an error.

Without this, a half-written final reply parsed to no status and the
delegation came back as "done".
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from apps.agents.tools.artifact_manager_agent import create_artifact_manager_tool
from apps.agents.tools.canvas_manager_agent import create_canvas_manager_tool

CUT_OFF = AIMessage(
    content=[{"type": "text", "text": '{"status": "done", "artifact_id": "a'}],
    response_metadata={"stop_reason": "max_tokens"},
)
REFUSED = AIMessage(content=[], response_metadata={"stop_reason": "refusal"})


def _fake_llm(response):
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    llm = MagicMock()
    llm.bind_tools.return_value = bound
    return llm


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "detail"),
    [(CUT_OFF, "ran out of output tokens"), (REFUSED, "declined the request")],
)
async def test_artifact_manager_reports_cut_off_as_error(response, detail):
    prefix = "apps.agents.tools.artifact_manager_agent"
    with (
        patch("apps.agents.graph.nested.ChatAnthropic", return_value=_fake_llm(response)),
        patch(f"{prefix}.create_artifact_graph_tools", return_value=[]),
    ):
        manager = create_artifact_manager_tool(SimpleNamespace(id="ws-1"), None, [])
        result = await manager.ainvoke({"task": "Build a dashboard."})

    assert result["status"] == "error"
    assert detail in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "detail"),
    [(CUT_OFF, "ran out of output tokens"), (REFUSED, "declined the request")],
)
async def test_canvas_manager_reports_cut_off_as_error(response, detail):
    prefix = "apps.agents.tools.canvas_manager_agent"
    with (
        patch("apps.agents.graph.nested.ChatAnthropic", return_value=_fake_llm(response)),
        patch(f"{prefix}.create_canvas_tools", return_value=[]),
    ):
        manager = create_canvas_manager_tool(SimpleNamespace(id="ws-1"), None, [], "thread")
        result = await manager.ainvoke(
            {"task": "Add a topic field.", "subagent_event_queue": asyncio.Queue()}
        )

    assert result["status"] == "error"
    assert result["error_code"] == "MODEL_STOPPED"
    assert detail in result["message"]
