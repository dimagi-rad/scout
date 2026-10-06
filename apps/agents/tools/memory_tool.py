"""Tools for saving to personal and workspace memory (#849)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

from apps.memory.models import WorkspaceMemoryEvent
from apps.memory.services import MemoryValidationError, asave_personal_memory
from apps.memory.workspace import asave_workspace_memory
from apps.workspaces.access import aworkspace_write_allowed, tool_write_denied

if TYPE_CHECKING:
    from apps.users.models import User
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)

PERSONAL_MEMORY_TOOL_NAME = "save_personal_memory"
WORKSPACE_MEMORY_TOOL_NAME = "save_workspace_memory"


def create_personal_memory_tool(user: User):
    """Bind the tool to ``user`` so the model can only ever write that user's rows.

    It needs no workspace role: the row is private to the user, so read-only
    members get it too.
    """

    @tool(PERSONAL_MEMORY_TOOL_NAME)
    async def save_personal_memory(memory: str) -> dict[str, Any]:
        """Save one durable preference of this user to their personal memory.

        Personal memory follows the user into every future conversation in every
        workspace, and only they can see it. Use it only when the user asks you
        to remember something, or states a clearly lasting preference about how
        they want answers (formats, layouts, units, habits). Never use it for a
        one-off instruction, for facts about a dataset, or for anything sensitive.

        Args:
            memory: One short, self-contained sentence in the third person, e.g.
                "Show district totals as a table, sorted by district name."

        Returns:
            A dict with status ("saved", "already_saved" or "error"), layer
            ("personal"), the saved memory text and a message.
        """
        try:
            result = await asave_personal_memory(user, memory)
        except MemoryValidationError as exc:
            return {"status": "error", "layer": "personal", "message": str(exc)}
        except Exception:
            # ToolNode re-raises anything but a ToolInvocationError, which would end
            # the user's turn over a memory save.
            logger.exception("Failed to save personal memory for user %s", user.pk)
            return {
                "status": "error",
                "layer": "personal",
                "message": "Saving to memory failed. Tell the user it was not saved.",
            }
        logger.info(
            "Personal memory %s for user %s (created=%s)",
            result.memory.id,
            user.pk,
            result.created,
        )
        return {
            "status": "saved" if result.created else "already_saved",
            "layer": "personal",
            "memory_id": str(result.memory.id),
            "memory": result.memory.content,
            "message": (
                "Saved to the user's personal memory; it applies from their next message."
                if result.created
                else "The user's personal memory already has this."
            ),
        }

    return save_personal_memory


def create_workspace_memory_tool(workspace: Workspace, user: User):
    """Bind the tool to ``workspace`` and ``user``; it checks the user's role on every call.

    Offered in every chat, read-only ones included, so the agent can tell a read-only
    member why it can't save; the role check here is what enforces it.
    """

    @tool(WORKSPACE_MEMORY_TOOL_NAME)
    async def save_workspace_memory(memory: str, tables: list[str] | None = None) -> dict[str, Any]:
        """Save a fact about how this workspace's data should be combined or interpreted.

        Workspace memory is shared: it applies in every member's future conversations in
        this workspace, and every member can see it on the Memory page. Use it when the
        user asks you to remember something about this data, or after you confirm a
        lasting fact about it, such as a correction to how a field must be read, a
        required filter, or the right way to combine two datasets. Never use it for a
        personal formatting preference (use save_personal_memory), a one-off
        instruction, a guess, or anything sensitive.

        Args:
            memory: One self-contained statement, specific enough to apply later, e.g.
                "Visits with status 'test' are training data; exclude them from counts."
            tables: Optional table or dataset names the memory is about.

        Returns:
            A dict with status ("saved", "already_saved", "denied" or "error"), layer
            ("workspace"), the saved memory text and a message.
        """
        if not await aworkspace_write_allowed(user, workspace.id):
            return {**tool_write_denied(), "layer": "workspace"}
        try:
            result = await asave_workspace_memory(
                workspace, user, memory, tables, source=WorkspaceMemoryEvent.Source.CHAT
            )
        except MemoryValidationError as exc:
            return {"status": "error", "layer": "workspace", "message": str(exc)}
        except Exception:
            logger.exception("Failed to save workspace memory for workspace %s", workspace.id)
            return {
                "status": "error",
                "layer": "workspace",
                "message": "Saving to memory failed. Tell the user it was not saved.",
            }
        return {
            "status": "saved" if result.created else "already_saved",
            "layer": "workspace",
            "memory_id": str(result.memory.id),
            "memory": result.memory.description,
            "tables": result.memory.applies_to_tables,
            "message": (
                "Saved to this workspace's shared memory; it applies from the next message."
                if result.created
                else "This workspace's memory already has this."
            ),
        }

    return save_workspace_memory
