"""Expand chat attachments into model content blocks at call time.

The checkpoint keeps a HumanMessage's content as the typed text and lists its
attachments in ``additional_kwargs``; only the copy sent to the model carries the
bytes, so checkpoints, the UI history and the graph's state stay small.
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass

import anthropic
from django.utils import timezone
from langchain_core.messages import BaseMessage, HumanMessage

try:
    # Private, so guarded: without it a rejection stubs every image in the request.
    from langchain_anthropic.chat_models import _format_messages
except ImportError:  # pragma: no cover
    _format_messages = None

from apps.chat.attachments import message_attachments
from apps.chat.models import ChatAttachment

INLINE_TEXT_MAX_CHARS = 100_000
TRUNCATED_TEXT_CHARS = 20_000
MIN_SHOWN_TEXT_CHARS = 2_000
# Per model request, newest attachments first. Anthropic caps a request at 32 MB,
# and base64 adds a third to every image. Text is budgeted conservatively: numeric
# CSV runs near 2 characters a token, so 100k characters stays under ~50k tokens of
# a window it shares with the history and tool results.
TEXT_BUDGET_CHARS = 100_000
IMAGE_BUDGET_BYTES = 15 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 20


@dataclass(frozen=True)
class LoadedAttachment:
    id: str
    filename: str
    media_type: str
    rejected: bool = False
    # Exactly one is set: an image's bytes, or a text file's decoded text.
    image: bytes | None = None
    text: str | None = None


def _loaded(row: ChatAttachment) -> LoadedAttachment:
    content = bytes(row.blob.content)
    common = {
        "id": str(row.id),
        "filename": row.filename,
        "media_type": row.media_type,
        "rejected": row.rejected_at is not None,
    }
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
        self._base64: dict[str, str] = {}
        # Images the API refused this run, and those the latest expansion sent.
        self.rejected_images: set[str] = set()
        self.sent_images: set[str] = set()

    async def areject_images(self, exc: BaseException, messages: list) -> bool:
        """Mark the image the API refused as unusable, here and for later turns.

        The error names the block (``messages.N.content.M.image…``); when that cannot
        be mapped to an attachment, every image the request carried is marked.
        False when the request carried no image to blame.
        """
        ids = self._named_image(exc, messages) or set(self.sent_images)
        if not ids:
            return False
        self.rejected_images |= ids
        await ChatAttachment.objects.filter(id__in=list(ids)).aupdate(rejected_at=timezone.now())
        return True

    def _named_image(self, exc: BaseException, messages: list) -> set[str]:
        match = _IMAGE_BLOCK_PATH.search(str(exc))
        if match is None or _format_messages is None:
            return set()
        try:
            # The index counts the API's messages, after langchain merges and drops
            # the system message, so resolve it the same way it formats them.
            _system, formatted = _format_messages(messages)
            block = formatted[int(match.group(1))]["content"][int(match.group(2))]
            data = block["source"]["data"]
        except (IndexError, KeyError, TypeError, ValueError):
            return set()
        return {i for i in self.sent_images if self._base64.get(i) == data}

    def base64_of(self, attachment: LoadedAttachment) -> str:
        """The image's base64, encoded once per run rather than on every model call."""
        if attachment.id not in self._base64:
            self._base64[attachment.id] = base64.b64encode(attachment.image or b"").decode()
        return self._base64[attachment.id]

    async def aload(self, ids: list[str]) -> dict[str, LoadedAttachment | None]:
        missing = [i for i in dict.fromkeys(ids) if i not in self._cache]
        if missing and self.thread_id:
            async for row in ChatAttachment.objects.filter(
                id__in=missing, thread_id=self.thread_id
            ).select_related("blob"):
                self._cache[str(row.id)] = _loaded(row)
        for attachment_id in missing:
            self._cache.setdefault(attachment_id, None)
        return {i: self._cache[i] for i in ids}


def safe_filename(filename: str | None) -> str:
    """A user-chosen filename as it may appear in model-facing framing.

    Escaped and without brackets, so a name cannot pose as the notes
    around it.
    """
    name = (filename or "attachment").replace("[", "(").replace("]", ")")
    return html.escape(name, quote=True)


def _label(entry: dict) -> str:
    return f'"{safe_filename(entry.get("filename"))}" (id {entry["id"]})'


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


_ATTACHMENT_TAG = re.compile(r"<(/?)(attachment)", re.IGNORECASE)


def wrap_attachment(attachment: LoadedAttachment, body: str, total_chars: int) -> str:
    filename = safe_filename(attachment.filename)
    # The tags mark where user-supplied content ends; a tag inside the file must not
    # end it early or open a second one under another name.
    body = _ATTACHMENT_TAG.sub(r"<\\\1\2", body)
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
        f"[Image {_label(entry)} is not shown: this request already carries as many "
        "images as it can. If you need to see it, ask the user to attach it again "
        "in a message with fewer images.]"
    )


def _text_omitted(entry: dict) -> dict:
    return _text_block(
        f"[Text file {_label(entry)} is not shown: this request already carries as "
        "much attached text as it can. Ask the user to attach it again on its own if "
        "you need it.]"
    )


async def _attachment_block(entry: dict, loader: AttachmentLoader, budget: _Budget) -> dict:
    # Checked against the stored metadata first, so an image over budget or a text
    # file past the text budget is never loaded. A text file within it is loaded
    # whole, even when only its head is shown.
    is_image = str(entry.get("media_type", "")).startswith("image/")
    known_size = entry.get("size") if isinstance(entry.get("size"), int) else 0
    if is_image and (budget.images <= 0 or known_size > budget.image_bytes):
        return _image_omitted(entry)
    if not is_image and budget.text_chars < MIN_SHOWN_TEXT_CHARS:
        return _text_omitted(entry)
    if is_image and entry["id"] in loader.rejected_images:
        return _text_block(f"[Image {_label(entry)} could not be processed.]")

    attachment = (await loader.aload([entry["id"]]))[entry["id"]]
    if attachment is None:
        return _text_block(f"[Attachment {_label(entry)} is no longer available.]")
    if attachment.image is not None:
        size = len(attachment.image)
        if budget.images <= 0 or size > budget.image_bytes:
            return _image_omitted(entry)
        budget.images -= 1
        budget.image_bytes -= size
        if attachment.rejected:
            return _text_block(f"[Image {_label(entry)} could not be processed.]")
        loader.sent_images.add(attachment.id)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": attachment.media_type,
                "data": loader.base64_of(attachment),
            },
        }

    text = attachment.text or ""
    total = len(text)
    if total <= INLINE_TEXT_MAX_CHARS and total <= budget.text_chars:
        budget.text_chars -= total
        return _text_block(wrap_attachment(attachment, text, total))
    shown = min(TRUNCATED_TEXT_CHARS, budget.text_chars)
    if shown < MIN_SHOWN_TEXT_CHARS:
        # A few hundred characters of a large file mislead more than they inform.
        return _text_omitted(entry)
    budget.text_chars -= shown
    note = f"[Showing the first {shown:,} of {total:,} characters; the rest is not shown.]"
    return _text_block(wrap_attachment(attachment, f"{text[:shown]}\n{note}", total))


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
    loader.sent_images = set()
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


_IMAGE_BLOCK_PATH = re.compile(r"messages\.(\d+)\.content\.(\d+)\.image")


def is_image_rejection(exc: BaseException) -> bool:
    """Whether the API refused the request over an image block (a 400 naming it)."""
    return isinstance(exc, anthropic.BadRequestError) and "image" in str(exc).lower()
