"""Shared helpers for chat views."""

import logging

from langchain_core.messages import AIMessage, ToolMessage

from apps.users.decorators import (  # noqa: F401 — re-exported for backwards compat
    LoginRequiredJsonMixin,
    async_login_required,
    get_user_if_authenticated,
    login_required_json,
)
from apps.workspaces.models import WorkspaceMembership

logger = logging.getLogger(__name__)


class CheckpointerUnavailable(Exception):
    """Raised when the LangGraph checkpointer can't be reached/read.

    Distinguishes a genuine outage (DB/checkpointer blip) from a thread that is
    legitimately empty (no checkpoint written yet). Callers that load message
    history must surface this as an error (non-200) rather than swallowing it
    into an empty result that reads as "conversation deleted" (arch #256, 07#7).
    """


async def repair_dangling_tool_calls(agent, config) -> list[ToolMessage]:
    """Return synthetic ToolMessages for any tool_use calls that were never answered.

    When a user sends a new message while a tool is still in-flight, the
    checkpointed history ends with an AIMessage containing tool_calls that
    have no corresponding ToolMessages. Anthropic's API rejects such sequences
    with HTTP 400.

    This function loads the current checkpoint, detects those dangling calls,
    and returns a ``ToolMessage`` for each one so the caller can prepend them
    to the next ``input_state["messages"]`` before streaming.

    Args:
        agent: A compiled LangGraph agent with a checkpointer.
        config: The LangGraph config dict (must contain ``configurable.thread_id``).

    Returns:
        A (possibly empty) list of synthetic ToolMessages to inject.
    """
    try:
        state = await agent.aget_state(config)
    except Exception:
        logger.warning("repair_dangling_tool_calls: could not load checkpoint state", exc_info=True)
        return []

    # Unexpected shape (e.g. fully-mocked agent in tests, or a checkpointer that
    # can't load the thread): fall back quietly.
    values = getattr(state, "values", None)
    if not isinstance(values, dict):
        return []
    messages = values.get("messages") or []
    if not isinstance(messages, list):
        return []

    answered_ids: set[str] = {
        msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage) and msg.tool_call_id
    }

    dangling: list[ToolMessage] = []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", []) or []:
                tc_id = tc.get("id")
                if tc_id and tc_id not in answered_ids:
                    logger.warning(
                        "repair_dangling_tool_calls: injecting synthetic tool_result "
                        "for dangling tool_call_id=%s tool_name=%s",
                        tc_id,
                        tc.get("name", "unknown"),
                    )
                    dangling.append(
                        ToolMessage(
                            content=(
                                "Tool call was interrupted — the user sent a new message "
                                "before this tool completed. Please acknowledge this and "
                                "respond to the user's latest message."
                            ),
                            tool_call_id=tc_id,
                            name=tc.get("name", "unknown"),
                        )
                    )
            break  # only inspect the most recent AIMessage

    return dangling


async def _resolve_workspace_and_membership(user, workspace_id):
    """Resolve workspace access for a user.

    Returns (workspace, tenant_membership, is_multi_tenant):
    - (None, None, False): workspace not found or user lacks WorkspaceMembership
    - (workspace, None, True): multi-tenant workspace; WorkspaceMembership is sufficient
    - (workspace, None, False): single-tenant workspace but user lacks TenantMembership
    - (workspace, tm, False): single-tenant workspace with a valid TenantMembership
    """
    try:
        wm = await WorkspaceMembership.objects.select_related("workspace").aget(
            workspace_id=workspace_id, user=user
        )
    except WorkspaceMembership.DoesNotExist:
        return None, None, False

    workspace = wm.workspace

    is_multi_tenant = await workspace.workspace_tenants.acount() > 1
    if is_multi_tenant:
        return workspace, None, True

    tenant = await workspace.tenants.afirst()
    if tenant is None:
        return workspace, None, False

    from apps.users.models import TenantMembership

    try:
        tm = await TenantMembership.objects.aget(user=user, tenant=tenant)
    except TenantMembership.DoesNotExist:
        return workspace, None, False
    return workspace, tm, False
