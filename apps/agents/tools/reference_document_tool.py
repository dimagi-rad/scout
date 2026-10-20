"""Tool for reading workspace reference documents on demand (#913)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

from apps.knowledge.services.reference_documents import (
    DEFAULT_READ_LIMIT,
    READ_REFERENCE_DOCUMENT_TOOL_NAME,
    ReferenceDocumentError,
    alist_reference_documents,
    aresolve_reference_document,
    read_page,
    search_document,
)

if TYPE_CHECKING:
    from apps.workspaces.models import Workspace

logger = logging.getLogger(__name__)


def create_reference_document_tool(workspace: Workspace):
    """Bind the tool to ``workspace``: it can only ever read that workspace's documents.

    Every member may read them, so it needs no role check.
    """

    @tool(READ_REFERENCE_DOCUMENT_TOOL_NAME)
    async def read_reference_document(
        title_or_id: str,
        offset: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Read a reference document that a member uploaded to this workspace.

        Reference documents (registries, code lists, definitions) are listed by title
        under "Reference Documents" in your instructions; their contents are only
        available through this tool. Prefer `search` to find the entries you need;
        page through with `offset` only when you need the whole document.

        Args:
            title_or_id: The document's title as listed (a unique part of it works), or its id.
            offset: Character offset to start reading from. Use the previous call's
                `next_offset` to read the next page.
            limit: Maximum characters to return (default 20000, at most 50000). With
                `search`, it caps the total size of the matches returned.
            search: Words to look for, case-insensitive; every word must appear in the
                same entry. A JSON document returns the matching entries with their JSON
                path; any other document returns the matching lines with line numbers
                and surrounding lines.

        Returns:
            A dict with the document's id, title, format and total_chars, plus either a
            page of content (offset, end_offset, next_offset; next_offset is null at the
            end) or the search matches. On error, status "error" and the documents you
            can read.
        """
        try:
            document, candidates = await aresolve_reference_document(workspace, title_or_id)
            if document is None:
                return {
                    "status": "error",
                    "message": (
                        f"Several reference documents match {title_or_id!r}; pass one id."
                        if candidates
                        else f"No reference document matches {title_or_id!r}."
                    ),
                    "documents": (
                        [{"id": str(doc.id), "title": doc.title} for doc in candidates]
                        if candidates
                        else await alist_reference_documents(workspace)
                    ),
                }
            header = {
                "status": "ok",
                "id": str(document.id),
                "title": document.title,
                "format": document.source_format or "text",
            }
            # Parsing and searching up to 2 MB is CPU work; keep it off the event loop.
            if search:
                found = await asyncio.to_thread(search_document, document, search, limit=limit)
                return {**header, **found}
            return {**header, **read_page(document.content, offset, limit)}
        except ReferenceDocumentError as exc:
            return {"status": "error", "message": exc.user_message}
        except Exception:
            # ToolNode re-raises anything but a ToolInvocationError, which would end the turn.
            logger.exception("Failed to read reference document in workspace %s", workspace.id)
            return {"status": "error", "message": "Reading the document failed."}

    return read_reference_document
