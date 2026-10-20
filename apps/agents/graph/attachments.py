"""Expand chat attachments into model content blocks at call time.

The checkpoint keeps a HumanMessage's content as the typed text and lists its
attachments in ``additional_kwargs``; only the copy sent to the model carries the
bytes, so checkpoints, the UI history and the graph's state stay small.
"""

from __future__ import annotations

import base64
import html
import logging
import re
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, HumanMessage

from apps.chat.attachments import message_attachments
from apps.chat.models import ChatAttachment

logger = logging.getLogger(__name__)

INLINE_TEXT_MAX_CHARS = 100_000
TRUNCATED_TEXT_CHARS = 20_000
# Per model request, newest attachments first. Anthropic caps a request at 32 MB,
# and base64 adds a third to every image.
TEXT_BUDGET_CHARS = 300_000
IMAGE_BUDGET_BYTES = 15 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 20


@dataclass(frozen=True)
class LoadedAttachment:
    id: str
    filename: str
    media_type: str
    # Exactly one is set: an image's bytes, or a text file's decoded text.
    image: bytes | None = None
    text: str | None = None


def _loaded(row: ChatAttachment) -> LoadedAttachment:
    content = bytes(row.blob.content)
    common = {"id": str(row.id), "filename": row.filename, "media_type": row.media_type}
    if row.media_type.startswith("image/"):
        return LoadedAttachment(**common, image=content)
    # Validated as UTF-8 on upload; replace rather than fail a turn over a bad byte.
    return LoadedAttachment(**common, text=content.decode("utf-8", errors="replace"))


class AttachmentLoader:
    """Loads a thread's attachments once per agent run.

    Scoped to the thread: an id in a message the thread did not send (or one whose
    thread was deleted) loads as missing, never as another thread's file.
    """

    def __init__(self, thread_id: str | None):
        self.thread_id = thread_id
        self._cache: dict[str, LoadedAttachment | None] = {}

    async def aload(self, ids) -> dict[str, LoadedAttachment | None]:
        missing = [i for i in dict.fromkeys(ids) if i not in self._cache]
        if missing and self.thread_id:
            async for row in ChatAttachment.objects.filter(
                id__in=missing, thread_id=self.thread_id
            ).select_related("blob"):
                self._cache[str(row.id)] = _loaded(row)
        for attachment_id in missing:
            self._cache.setdefault(attachment_id, None)
        return {i: self._cache[i] for i in ids}


def _label(entry: dict) -> str:
    return f"{entry.get('filename') or 'attachment'} (id {entry['id']})"


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


_CLOSING_TAG = re.compile(r"</(attachment)", re.IGNORECASE)


def _wrap(attachment: LoadedAttachment, body: str, total_chars: int) -> str:
    filename = html.escape(attachment.filename, quote=True)
    # The tags mark where user-supplied content ends; a closing tag inside the file
    # must not end it early and pass what follows off as framing.
    body = _CLOSING_TAG.sub(r"<\\/\1", body)
    return (
        f'<attachment filename="{filename}" id="{attachment.id}" '
        f'media_type="{attachment.media_type}" chars="{total_chars}">\n{body}\n</attachment>'
    )


class _Budget:
    def __init__(self):
        self.text_chars = TEXT_BUDGET_CHARS
        self.image_bytes = IMAGE_BUDGET_BYTES
        self.images = MAX_IMAGES_PER_REQUEST


def _image_omitted(entry: dict) -> dict:
    return _text_block(
        f"[Image {_label(entry)} was attached earlier in this chat and is not shown "
        "again, to keep the request small. Ask the user to attach it again if you "
        "need to look at it.]"
    )


def _text_omitted(entry: dict) -> dict:
    return _text_block(
        f"[Text file {_label(entry)} was attached earlier in this chat and is not shown "
        "again, to keep the request small. Ask the user to attach it again if you need it.]"
    )


async def _attachment_block(entry: dict, loader: AttachmentLoader, budget: _Budget) -> dict:
    # Budgets are checked against the stored metadata first, so an attachment that
    # will not be shown is never loaded into memory.
    is_image = str(entry.get("media_type", "")).startswith("image/")
    known_size = entry.get("size") if isinstance(entry.get("size"), int) else 0
    if is_image and (budget.images <= 0 or known_size > budget.image_bytes):
        return _image_omitted(entry)
    if not is_image and budget.text_chars <= 0:
        return _text_omitted(entry)

    attachment = (await loader.aload([entry["id"]]))[entry["id"]]
    if attachment is None:
        return _text_block(f"[Attachment {_label(entry)} is no longer available.]")
    if attachment.image is not None:
        size = len(attachment.image)
        if budget.images <= 0 or size > budget.image_bytes:
            return _image_omitted(entry)
        budget.images -= 1
        budget.image_bytes -= size
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": attachment.media_type,
                "data": base64.b64encode(attachment.image).decode("ascii"),
            },
        }

    text = attachment.text or ""
    total = len(text)
    if total <= INLINE_TEXT_MAX_CHARS and total <= budget.text_chars:
        budget.text_chars -= total
        return _text_block(_wrap(attachment, text, total))
    shown = min(TRUNCATED_TEXT_CHARS, budget.text_chars)
    budget.text_chars -= shown
    note = f"[Showing the first {shown:,} of {total:,} characters; the rest is not shown.]"
    return _text_block(_wrap(attachment, f"{text[:shown]}\n{note}", total))


async def aexpand_attachments(
    messages: list[BaseMessage], loader: AttachmentLoader
) -> list[BaseMessage]:
    """``messages`` with each attachment-carrying HumanMessage turned into content blocks.

    Attachments come before the typed text, as Anthropic recommends for images.
    Budgets are spent newest message first, so an old upload is what gets left out.
    """
    carrying = [
        (index, entries)
        for index, message in enumerate(messages)
        if isinstance(message, HumanMessage) and (entries := message_attachments(message))
    ]
    if not carrying:
        return messages
    budget = _Budget()
    expanded = list(messages)
    for index, entries in reversed(carrying):
        message = messages[index]
        blocks = [await _attachment_block(e, loader, budget) for e in entries]
        if isinstance(message.content, list):
            blocks.extend(message.content)
        elif message.content.strip():
            blocks.append(_text_block(message.content))
        expanded[index] = message.model_copy(update={"content": blocks, "additional_kwargs": {}})
    return expanded
