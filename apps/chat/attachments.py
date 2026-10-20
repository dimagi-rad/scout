"""Chat attachments: validation, deduplicated storage and garbage collection.

Bytes live in ``AttachmentBlob`` keyed by (sha256, size), so the same screenshot
pasted twice is stored once. Callers only ever reach a blob through a
``ChatAttachment`` row they own.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePath

from django.db import IntegrityError
from django.db.models import Exists, OuterRef, ProtectedError
from django.utils import timezone

from apps.chat.models import AttachmentBlob, ChatAttachment

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 10
# Unsent uploads a user may hold at once; GC frees them after UNSENT_TTL.
MAX_UNSENT_PER_USER = 100
UNSENT_TTL = timedelta(hours=24)
# A blob is collectable only once it has had time to gain its first reference.
ORPHAN_BLOB_GRACE = timedelta(hours=1)
MAX_FILENAME_LENGTH = 255

IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
TEXT_MEDIA_TYPES_BY_EXTENSION = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
_TEXT_MEDIA_TYPE_ALIASES = {
    "application/json": "application/json",
    "text/csv": "text/csv",
    "text/tab-separated-values": "text/tab-separated-values",
    "text/plain": "text/plain",
    "text/markdown": "text/markdown",
    "text/x-markdown": "text/markdown",
}
TEXT_MEDIA_TYPES = frozenset(TEXT_MEDIA_TYPES_BY_EXTENSION.values())

ATTACHMENT_URL_PREFIX = "/api/chat/attachments/"


class AttachmentRejected(Exception):
    """An upload the server will not store; ``reason`` is the API's error code."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ValidatedFile:
    content: bytes
    media_type: str
    filename: str


def sniff_image_type(head: bytes) -> str | None:
    """The image media type the leading bytes prove, or None.

    Magic bytes rather than the client's claim: a renamed file must not reach the
    model as an image block it cannot decode.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_filename(raw: str | None) -> str:
    name = PurePath((raw or "").replace("\\", "/")).name
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    if not name:
        return "attachment"
    if len(name) > MAX_FILENAME_LENGTH:
        suffix = PurePath(name).suffix[:16]
        name = name[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name


def _base_media_type(claimed: str | None) -> str:
    return (claimed or "").split(";", 1)[0].strip().lower()


def validate_upload(content: bytes, *, filename: str | None, claimed_type: str | None):
    """Decide what an upload is, or raise ``AttachmentRejected``.

    Images are recognised by content alone; anything else must be one of the
    allowed text formats (by extension, or by its claimed type when the name has
    none) and decode as UTF-8.
    """
    name = clean_filename(filename)
    if not content:
        raise AttachmentRejected("attachment_invalid", "The file is empty.")

    image_type = sniff_image_type(content[:16])
    if image_type is not None:
        if len(content) > MAX_IMAGE_BYTES:
            raise AttachmentRejected(
                "attachment_too_large",
                f"Images can be at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
            )
        return ValidatedFile(content=content, media_type=image_type, filename=name)

    claimed = _base_media_type(claimed_type)
    suffix = PurePath(name).suffix.lower()
    text_type = TEXT_MEDIA_TYPES_BY_EXTENSION.get(suffix)
    if text_type is None and not suffix:
        text_type = _TEXT_MEDIA_TYPE_ALIASES.get(claimed)
    if text_type is None:
        if claimed in IMAGE_MEDIA_TYPES or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            raise AttachmentRejected("attachment_invalid", "The image file is not readable.")
        raise AttachmentRejected(
            "attachment_type",
            "Attach PNG, JPEG, WebP or GIF images, or JSON, CSV, TSV, TXT or Markdown files.",
        )
    if len(content) > MAX_TEXT_BYTES:
        raise AttachmentRejected(
            "attachment_too_large",
            f"Text files can be at most {MAX_TEXT_BYTES // (1024 * 1024)} MB.",
        )
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        raise AttachmentRejected(
            "attachment_invalid", "Text files must be UTF-8 encoded."
        ) from None
    return ValidatedFile(content=content, media_type=text_type, filename=name)


async def _blob_id_for(content: bytes) -> int:
    sha256 = hashlib.sha256(content).hexdigest()
    existing = (
        await AttachmentBlob.objects.filter(sha256=sha256, size=len(content))
        .values_list("id", flat=True)
        .afirst()
    )
    if existing is not None:
        return existing
    try:
        blob = await AttachmentBlob.objects.acreate(
            sha256=sha256, size=len(content), content=content
        )
    except IntegrityError:
        # A concurrent upload of the same bytes created it first.
        return (
            await AttachmentBlob.objects.filter(sha256=sha256, size=len(content))
            .values_list("id", flat=True)
            .aget()
        )
    return blob.id


async def astore_attachment(validated: ValidatedFile, *, user, workspace) -> ChatAttachment:
    """Store the bytes (reusing an identical blob) and record this user's upload of them."""

    async def create(blob_id: int) -> ChatAttachment:
        return await ChatAttachment.objects.acreate(
            blob_id=blob_id,
            user=user,
            workspace=workspace,
            filename=validated.filename,
            media_type=validated.media_type,
        )

    try:
        return await create(await _blob_id_for(validated.content))
    except IntegrityError:
        # GC deleted the reused orphan blob between lookup and insert.
        return await create(await _blob_id_for(validated.content))


async def acount_unsent(user) -> int:
    return await ChatAttachment.objects.filter(user=user, thread__isnull=True).acount()


def attachment_url(attachment_id) -> str:
    return f"{ATTACHMENT_URL_PREFIX}{attachment_id}/"


_ATTACHMENT_URL_RE = re.compile(
    r"^(?:/[A-Za-z0-9._~\-/]*)?/api/chat/attachments/"
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/$"
)


def attachment_id_from_url(url) -> str | None:
    """The attachment id an attachment URL names, optionally behind a base path, else None."""
    if not isinstance(url, str):
        return None
    match = _ATTACHMENT_URL_RE.match(url)
    return match.group("id").lower() if match else None


def attachment_payload(attachment: ChatAttachment, size: int) -> dict:
    return {
        "id": str(attachment.id),
        "filename": attachment.filename,
        "mediaType": attachment.media_type,
        "size": size,
        "url": attachment_url(attachment.id),
    }


async def acollect_garbage(now=None) -> dict:
    """Delete unsent uploads past their TTL, then blobs nothing references."""
    now = now or timezone.now()
    unsent_deleted, _ = await ChatAttachment.objects.filter(
        thread__isnull=True, created_at__lt=now - UNSENT_TTL
    ).adelete()
    orphans = AttachmentBlob.objects.filter(
        ~Exists(ChatAttachment.objects.filter(blob=OuterRef("pk"))),
        created_at__lt=now - ORPHAN_BLOB_GRACE,
    )
    orphan_ids = [blob_id async for blob_id in orphans.values_list("id", flat=True)]
    blobs_deleted = 0
    # One at a time, so a blob an upload reuses mid-sweep spares only itself.
    for blob_id in orphan_ids:
        try:
            # defer: the delete collector loads the row, and content can be megabytes.
            deleted, _ = await orphans.filter(pk=blob_id).defer("content").adelete()
        except (IntegrityError, ProtectedError):
            logger.info("attachment GC: blob %s gained a reference mid-sweep", blob_id)
            continue
        blobs_deleted += deleted
    return {"attachments": unsent_deleted, "blobs": blobs_deleted}
