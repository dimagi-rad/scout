"""read_attachment: page through or search a text file attached to this chat (#913).

Large attachments are only partly inlined into the conversation, and pruned
messages drop theirs entirely; this tool reads them back on demand.
"""

from __future__ import annotations

import re
import uuid

from langchain_core.tools import tool

from apps.agents.graph.attachments import AttachmentLoader, safe_filename, wrap_attachment

READ_ATTACHMENT_TOOL_NAME = "read_attachment"
DEFAULT_LIMIT = 20_000
# Characters, not bytes: compact_tool_results shapes MCP tool output only, so this
# cap is what bounds a result (a non-ASCII file can exceed 50 KB of UTF-8).
MAX_LIMIT = 50_000
MAX_SEARCH_MATCHES = 50
# Characters shown either side of each match.
SEARCH_CONTEXT_CHARS = 120


def _search(text: str, needle: str, offset: int) -> str:
    # The match's own offset, not its line's: a minified JSON file is one line. A
    # regex rather than lower() keeps offsets in the original text's coordinates.
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    matches = []
    total = 0
    resume_at = offset
    for match in pattern.finditer(text, offset):
        total += 1
        if len(matches) >= MAX_SEARCH_MATCHES:
            continue
        position = match.start()
        start = max(0, position - SEARCH_CONTEXT_CHARS)
        end = min(len(text), match.end() + SEARCH_CONTEXT_CHARS)
        snippet = text[start:end].replace("\r", "").replace("\n", "\\n")
        line = text.count("\n", 0, position) + 1
        matches.append(f"offset {position} (line {line}): …{snippet}…")
        resume_at = match.end()
    after = f" after offset {offset}" if offset else ""
    if not total:
        return f"No matches for {needle!r}{after}."
    more = (
        f"\n[{total - MAX_SEARCH_MATCHES} more matches; search again with offset={resume_at}.]"
        if total > MAX_SEARCH_MATCHES
        else ""
    )
    return f"{total} matches{after}:\n" + "\n".join(matches) + more


def create_read_attachment_tool(conversation_id: str):
    """Bind the tool to the thread, so it reads only attachments sent in this chat."""
    # One per run, like the graph's: paging a file must not reload it on every call.
    loader = AttachmentLoader(conversation_id)

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
        characters to return (at most 50,000). With ``search``, returns each
        case-insensitive match from ``offset`` on, with its offset, line number and
        surrounding text, so you can read around it. Images cannot be read with this tool.
        """
        try:
            canonical_id = str(uuid.UUID(attachment_id))
        except (ValueError, TypeError, AttributeError):
            return "No such attachment in this chat."
        attachment = (await loader.aload([canonical_id]))[canonical_id]
        if attachment is None:
            return "No such attachment in this chat."
        name = f'"{safe_filename(attachment.filename)}"'
        if attachment.text is None:
            return (
                f"{name} is an image; it cannot be read as text. Ask the user to attach "
                "it again if you need to look at it."
            )

        text = attachment.text
        offset = max(0, int(offset))
        if search:
            return f"[{name}, {len(text):,} characters]\n" + _search(text, search, offset)
        if offset >= len(text):
            return f"[{name} has {len(text):,} characters; offset {offset:,} is past the end.]"
        end = min(len(text), offset + min(max(1, int(limit)), MAX_LIMIT))
        header = f"[{name}: characters {offset:,}-{end:,} of {len(text):,}]"
        footer = (
            f"\n[More remains: call read_attachment again with offset={end}.]"
            if end < len(text)
            else ""
        )
        return f"{header}\n{wrap_attachment(attachment, text[offset:end], len(text))}{footer}"

    return read_attachment
