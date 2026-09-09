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
        # Inline import breaks a verified cycle: graph.base -> this module ->
        # workspaces.tasks -> graph.base (tasks imports build_agent_graph for the
        # resume path). Module-level fails with a partially-initialized import.
        from apps.workspaces.tasks import materialize_workspace_blocking

        # Dedupe-aware: waits for any in-progress materialization on this
        # workspace's tenants rather than starting a parallel run.
        summary = await materialize_workspace_blocking(workspace_id, user_id, job_id)
        tenants = summary.get("tenants", [])
        loaded = sum(1 for t in tenants if t.get("success"))
        view_schema = summary.get("view_schema")
        view_ok = view_schema is None or view_schema.get("ok")

        not_loaded = [t.get("tenant") for t in tenants if not t.get("success")]
        # Only name sources we actually have names for — the branches below are
        # reachable with nothing to name, and a dangling "others did not: ."
        # invites the agent to invent one.
        named = f": {', '.join(str(t) for t in not_loaded)}" if not_loaded else ""
        all_loaded = bool(summary.get("all_succeeded"))

        if all_loaded and view_ok:
            status = "completed"
            message = "Data loaded successfully. Continue with the analysis."
        elif all_loaded:
            # Nothing left to load and still no queryable surface, so the view
            # build itself is broken (its exception is swallowed into
            # view_schema["ok"] and the Cube build is skipped behind it).
            #
            # Gated on all_loaded, NOT on `not view_ok` alone: when a tenant did
            # not load, build_view_schema fails *because* that tenant has no
            # ACTIVE schema, so the fix is the tenant, not the build.
            status = "failed"
            message = (
                "Every tenant loaded, but the workspace query layer (view schema) "
                "failed to build, so nothing is queryable: "
                f"{(view_schema or {}).get('error') or 'unknown error'}. Do NOT retry — "
                "tell the user a system-side fix is required."
            )
        elif loaded:
            status = "partial"
            message = (
                f"Some tenants loaded; others did not{named}. Proceed with the available "
                "data and tell the user which data sources are NOT in the results."
            )
            if not view_ok:
                # Not relaying view_schema["error"]: build_view_schema says "run a
                # data refresh", which cannot succeed until whatever stopped the
                # missing sources is fixed (#412). The run's own guidance, appended
                # below, is the advice that actually applies.
                message += (
                    " The workspace's combined query layer could not be rebuilt while "
                    "sources are missing, so query only the sources that loaded and do "
                    "not present results as spanning the whole workspace."
                )
        else:
            status = "failed"
            message = f"Materialization failed; no data was loaded{named}."

        # Advice comes from the run, keyed by error code — this tool must not
        # write its own (apps/common/errors.py: raise sites describe, one owner
        # advises).
        guidance = summary.get("guidance") or []
        if guidance:
            message += " " + " ".join(guidance)

        logger.info(
            "Headless materialization for workspace %s: status=%s, tenants_loaded=%d, "
            "tenants_not_loaded=%d",
            workspace_id,
            status,
            loaded,
            len(not_loaded),
        )
        return {
            "status": status,
            "tenants_loaded": loaded,
            "tenants_not_loaded": not_loaded,
            "message": message,
        }

    run_materialization.name = "run_materialization"
    return run_materialization


__all__ = ["create_materialization_tool"]
