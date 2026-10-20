"""Expand chat attachments into model content blocks at call time.

The checkpoint keeps a HumanMessage's content as the typed text and lists its
attachments in ``additional_kwargs``; only the copy sent to the model carries the
bytes, so checkpoints, the UI history and the graph's state stay small.
"""

from __future__ import annotations

import base64
import html
import re
import uuid
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
    # Exactly one is set: an image's bytes, or a text file's decoded text.
    image: bytes | None = None
    text: str | None = None


def _loaded(row: ChatAttachment) -> LoadedAttachment:
    content = bytes(row.blob.content)
    common = {
        "id": str(row.id),
        "filename": row.filename,
        "media_type": row.media_type,
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
        self._rejection_checked: set[str] = set()

    async def areject_images(self, exc: BaseException, messages: list) -> bool:
        """Mark the image the API refused as unusable; False when none was sent.

        The error names the block (``messages.N.content.M.image…``). The attachment
        it resolves to is marked for good, so later turns skip it up front. When it
        cannot be resolved, every image the request carried is left out for this run
        only: persisting a guess would hide good images for ever.
        """
        named = self._named_image(exc, messages)
        ids = named or set(self.sent_images)
        if not ids:
            return False
        self.rejected_images |= ids
        if named:
            await ChatAttachment.objects.filter(id__in=list(named)).aupdate(
                rejected_at=timezone.now()
            )
        return True

    async def aload_rejections(self, ids) -> None:
        """Note which of ``ids`` an earlier turn found the API refuses, without their bytes."""
        unchecked = [i for i in dict.fromkeys(ids) if i not in self._rejection_checked]
        if not unchecked or not self.thread_id:
            return
        self._rejection_checked.update(unchecked)
        self.rejected_images.update(
            [
                str(attachment_id)
                async for attachment_id in ChatAttachment.objects.filter(
                    id__in=unchecked, thread_id=self.thread_id, rejected_at__isnull=False
                ).values_list("id", flat=True)
            ]
        )

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
        if missing and _is_uuid(self.thread_id):
            async for row in ChatAttachment.objects.filter(
                id__in=missing, thread_id=self.thread_id
            ).select_related("blob"):
                self._cache[str(row.id)] = _loaded(row)
        for attachment_id in missing:
            self._cache.setdefault(attachment_id, None)
        return {i: self._cache[i] for i in ids}


def _is_uuid(value) -> bool:
    # A conversation id that is not a UUID owns no attachments; querying would raise.
    if value is None:
        return False
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


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


def neutralise_tags(text: str) -> str:
    """File text with any <attachment> or </attachment> made inert."""
    return _ATTACHMENT_TAG.sub(r"<\\\1\2", text)


def wrap_attachment(
    attachment: LoadedAttachment, body: str, total_chars: int, *, start: int = 0
) -> str:
    """``body`` (characters ``start`` on of a ``total_chars`` file) inside the tags."""
    filename = safe_filename(attachment.filename)
    # The tags mark where user-supplied content ends; a tag inside the file must not
    # end it early or open a second one under another name.
    shown = f'range="{start}-{start + len(body)}" ' if start or len(body) < total_chars else ""
    body = neutralise_tags(body)
    return (
        f'<attachment filename="{filename}" id="{attachment.id}" '
        f'media_type="{attachment.media_type}" chars="{total_chars}" {shown}'.rstrip()
        + f">\n{body}\n</attachment>"
    )


class _Budget:
    def __init__(self):
        self.text_chars = TEXT_BUDGET_CHARS
        self.image_bytes = IMAGE_BUDGET_BYTES
        self.images = MAX_IMAGES_PER_REQUEST


def _image_refused(entry: dict) -> dict:
    return _text_block(
        f"[Image {_label(entry)} could not be processed by the model. If it matters, "
        "ask the user to attach it again, for example as a fresh PNG or JPEG screenshot.]"
    )


def _image_omitted(entry: dict) -> dict:
    return _text_block(
        f"[Image {_label(entry)} is not shown: this request already carries as many "
        "images as it can. If you need to see it, ask the user to attach it again "
        "in a message with fewer images.]"
    )


def _text_omitted(entry: dict) -> dict:
    return _text_block(
        f"[Text file {_label(entry)} is not shown: this request already carries as "
        "much attached text as it can. Call read_attachment with that id to read it.]"
    )


async def _attachment_block(entry: dict, loader: AttachmentLoader, budget: _Budget) -> dict:
    # Checked against the stored metadata first, so an image over budget, or any text
    # file once under MIN_SHOWN_TEXT_CHARS of text budget is left, is never loaded.
    # A text file admitted is loaded whole, even when only its head is shown.
    is_image = str(entry.get("media_type", "")).startswith("image/")
    # Before any budget: a refused image spends none, and its bytes are never loaded.
    if is_image and entry["id"] in loader.rejected_images:
        return _image_refused(entry)
    known_size = entry.get("size") if isinstance(entry.get("size"), int) else 0
    if is_image and (budget.images <= 0 or known_size > budget.image_bytes):
        return _image_omitted(entry)
    if not is_image and budget.text_chars < MIN_SHOWN_TEXT_CHARS:
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
    note = (
        f"[Showing the first {shown:,} of {total:,} characters. Call read_attachment with "
        f'attachment_id="{attachment.id}" and offset={shown} to read on, or with search '
        "to find something in it.]"
    )
    # After the closing tag: inside it, the pointer would read as file content.
    return _text_block(f"{wrap_attachment(attachment, text[:shown], total)}\n{note}")


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
    await loader.aload_rejections(
        [
            e["id"]
            for _, entries in carrying
            for e in entries
            if str(e.get("media_type", "")).startswith("image/")
        ]
    )
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
