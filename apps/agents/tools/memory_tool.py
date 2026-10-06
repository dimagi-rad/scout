"""Tool for saving a user's durable preferences to their personal memory (#849)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

from apps.memory.services import MemoryValidationError, asave_personal_memory

if TYPE_CHECKING:
    from apps.users.models import User

logger = logging.getLogger(__name__)

PERSONAL_MEMORY_TOOL_NAME = "save_personal_memory"


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
