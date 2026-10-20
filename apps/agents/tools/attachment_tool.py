"""read_attachment: page through or search a text file attached to this chat (#913).

Large attachments are only partly inlined into the conversation, and pruned
messages drop theirs entirely; this tool reads them back on demand.
"""

from __future__ import annotations

import uuid

from langchain_core.tools import tool

from apps.agents.graph.attachments import AttachmentLoader

READ_ATTACHMENT_TOOL_NAME = "read_attachment"
DEFAULT_LIMIT = 20_000
MAX_LIMIT = 50_000
MAX_SEARCH_MATCHES = 50
MAX_MATCH_LINE_CHARS = 300


def _search(text: str, needle: str, offset: int) -> str:
    lowered = needle.lower()
    matches = []
    total = 0
    position = 0
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        start = position
        position += len(line)
        if start + len(line) <= offset or lowered not in line.lower():
            continue
        total += 1
        if len(matches) < MAX_SEARCH_MATCHES:
            shown = line.rstrip("\r\n")
            if len(shown) > MAX_MATCH_LINE_CHARS:
                shown = shown[:MAX_MATCH_LINE_CHARS] + "…"
            matches.append(f"line {number} (offset {start}): {shown}")
    if not total:
        return f"No lines contain {needle!r}."
    more = (
        f"\n[{total - MAX_SEARCH_MATCHES} more matches; search again with a larger offset.]"
        if total > MAX_SEARCH_MATCHES
        else ""
    )
    return f"{total} matching lines:\n" + "\n".join(matches) + more


def create_read_attachment_tool(conversation_id: str):
    """Bind the tool to the thread, so it reads only attachments sent in this chat."""

    @tool(READ_ATTACHMENT_TOOL_NAME)
    async def read_attachment(
        attachment_id: str,
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
        search: str | None = None,
    ) -> str:
        """Read a text file (JSON, CSV, TSV, TXT, Markdown) the user attached to this chat.

        Use it when an attachment was shown only in part, or was attached in a message
        no longer in view. ``offset`` is a character offset and ``limit`` the number of
        characters to return (at most 50,000). With ``search``, returns the lines that
        contain it (case-insensitive) from ``offset`` on, with each line's offset, so you
        can read around a match. Images cannot be read with this tool.
        """
        try:
            canonical_id = str(uuid.UUID(attachment_id))
        except (ValueError, TypeError, AttributeError):
            return f"No attachment {attachment_id!r} in this chat."
        attachment = (await AttachmentLoader(conversation_id).aload([canonical_id]))[canonical_id]
        if attachment is None:
            return f"No attachment {attachment_id!r} in this chat."
        if attachment.text is None:
            return (
                f"{attachment.filename} is an image; it cannot be read as text. Ask the user "
                "to attach it again if you need to look at it."
            )

        text = attachment.text
        offset = max(0, int(offset))
        if search:
            return f"[{attachment.filename}, {len(text):,} characters]\n" + _search(
                text, search, offset
            )
        limit = min(max(1, int(limit)), MAX_LIMIT)
        end = min(len(text), offset + limit)
        header = f"[{attachment.filename}: characters {offset:,}-{end:,} of {len(text):,}]"
        if offset >= len(text):
            return f"{header}\n[Offset is past the end of the file.]"
        footer = (
            f"\n[More remains: call read_attachment again with offset={end}.]"
            if end < len(text)
            else ""
        )
        return f"{header}\n{text[offset:end]}{footer}"

    return read_attachment
