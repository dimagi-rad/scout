"""Headless blocking materialization tool for non-interactive agent runs.

The interactive chat agent uses the MCP ``run_materialization`` tool, which
fires a background job and acknowledges immediately, relying on a chat
``Thread`` + checkpointer + ``resume_thread_after_materialization`` to deliver
the result back into the conversation later.

A recipe run has none of that — it is a one-shot, thread-less, checkpointer-less
invocation. So it gets this tool instead: it runs the same materialization core
*inline* and BLOCKS until loading finishes, returning a completion summary the
agent can act on within the same run.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.tools import tool

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)


def create_materialization_tool(workspace: Workspace, user: User | None, job_id: int | None = None):
    """Factory for a headless, blocking ``run_materialization`` tool.

    The tool is named ``run_materialization`` (same as the interactive MCP tool)
    so the agent's behavior and prompts are mode-agnostic. It takes no
    LLM-facing arguments — ``workspace``/``user``/``job_id`` are bound here by
    closure, so the synthetic recipe ``thread_id`` is never involved.

    Args:
        workspace: The Workspace to materialize.
        user: The user on whose behalf the run executes (scopes memberships).
        job_id: The enclosing Procrastinate job id, recorded on the
            MaterializationRun for traceability. May be None outside a task.
    """
    workspace_id = str(workspace.id)
    user_id = str(user.id) if user else ""

    @tool
    async def run_materialization() -> dict:
        """Load or refresh this workspace's data from its source(s).

        Blocks until loading completes, then returns a status summary. Call this
        before querying when no data has been loaded yet. After it returns
        ``status: completed``, continue with the requested analysis in the same
        run — the data is ready.
        """
        # Imported inside the function to break a real import cycle (verified:
        # module-level fails with ImportError "partially initialized module
        # apps.workspaces.tasks" in every import order). The chain is
        # graph.base -> this module -> workspaces.tasks -> graph.base
        # (tasks imports build_agent_graph for the resume path). This mirrors the
        # established pattern in apps/agents/tools/recipe_tool.py, which imports
        # apps.recipes.models inside the tool body for the same reason.
        from apps.workspaces.tasks import materialize_workspace_core

        summary = await materialize_workspace_core(workspace_id, user_id, job_id)
        tenants = summary.get("tenants", [])
        loaded = sum(1 for t in tenants if t.get("success"))
        view_schema = summary.get("view_schema")
        view_ok = view_schema is None or view_schema.get("ok")

        if summary.get("all_succeeded") and view_ok:
            status = "completed"
            message = "Data loaded successfully. Continue with the analysis."
        elif loaded:
            status = "partial"
            message = (
                "Some tenants loaded; others failed. Proceed with the available data "
                "and note the gap to the user."
            )
        else:
            status = "failed"
            message = "Materialization failed; no data was loaded."

        logger.info(
            "Headless materialization for workspace %s: status=%s, tenants_loaded=%d",
            workspace_id,
            status,
            loaded,
        )
        return {"status": status, "tenants_loaded": loaded, "message": message}

    run_materialization.name = "run_materialization"
    return run_materialization


__all__ = ["create_materialization_tool"]
