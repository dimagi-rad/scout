"""Shared helpers for chat views."""

import logging

from langchain_core.messages import AIMessage, ToolMessage

from apps.agents.graph.state import all_tool_calls
from apps.users.decorators import (  # noqa: F401 — re-exported for backwards compat
    LoginRequiredJsonMixin,
    async_login_required,
    get_user_if_authenticated,
    login_required_json,
)
from apps.users.models import TenantMembership
from apps.workspaces.access import aresolve_workspace_access_ex

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
            for tc in all_tool_calls(msg):
                tc_id = tc.get("id")
                if tc_id and tc_id not in answered_ids:
                    logger.warning(
                        "repair_dangling_tool_calls: injecting synthetic tool_result "
                        "for dangling tool_call_id=%s tool_name=%s",
                        tc_id,
                        tc.get("name") or "unknown",
                    )
                    dangling.append(
                        ToolMessage(
                            content=(
                                "Tool call was interrupted — the user sent a new message "
                                "before this tool completed. Please acknowledge this and "
                                "respond to the user's latest message."
                            ),
                            tool_call_id=tc_id,
                            name=tc.get("name") or "unknown",
                        )
                    )
            break  # only inspect the most recent AIMessage

    return dangling


async def _resolve_chat_access(user, workspace_id):
    """Resolve workspace access plus the tenant flags the chat callers need.

    Access (WorkspaceMembership AND coverage of every tenant) is decided by the single
    authorizer. The full ``WorkspaceAccess`` is returned so a denial can be explained
    without resolving (and verifying upstream) again.

    Returns (access, tenant_membership, is_multi_tenant):
    - (access, None, False): no access (``access.workspace`` is None), or no tenant
    - (access, None, True): multi-tenant workspace (access already verified)
    - (access, tm, False): single-tenant workspace with the live TenantMembership
    """
    access = await aresolve_workspace_access_ex(user, workspace_id)
    workspace = access.workspace
    if workspace is None:
        return access, None, False

    is_multi_tenant = await workspace.workspace_tenants.acount() > 1
    if is_multi_tenant:
        # The authorizer already confirmed the user covers every tenant of this
        # workspace, so multi-tenant access is no longer WorkspaceMembership-only.
        return access, None, True

    tenant = await workspace.tenants.afirst()
    if tenant is None:
        return access, None, False
    tm = await TenantMembership.objects.filter(user=user, tenant=tenant).afirst()  # live-only
    return access, tm, False
