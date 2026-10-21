"""Chat attachments: validation, deduplicated storage and garbage collection.

Bytes live in ``AttachmentBlob`` keyed by (sha256, size), so the same screenshot
pasted twice is stored once. Callers only ever reach a blob through a
``ChatAttachment`` row they own, so no one can read another user's file. Dedup is
global, though, so an upload of bytes someone else already stored skips one insert
and returns slightly faster: a timing side channel that could at most confirm a
guessed file exists. Accepted as low impact; per-workspace keys would close it.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePath

from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, ProtectedError, Q
from django.utils import timezone

from apps.chat.image_headers import MalformedImage, read_image_header
from apps.chat.models import AttachmentBlob, ChatAttachment

logger = logging.getLogger(__name__)

# Within the direct Messages API's per-image limit (a 5 MB raw image was accepted live;
# the docs quote 10 MB of base64). Bedrock and Vertex quote 5 MB of base64, which
# would need about 3.75 MB raw; Scout calls the direct API.
# https://platform.claude.com/docs/en/build-with-claude/vision#request-limits
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# The API rejects an image over 8000 px on either side. Requests with more than 20
# images impose 2000 px; the model-call budget keeps them to 20
# (apps/agents/graph/attachments.py), so 8000 px is the limit that applies.
MAX_IMAGE_DIMENSION = 8000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 10
# File parts read per message, repeats included; a client sends each attachment once.
MAX_FILE_PARTS = 5 * MAX_ATTACHMENTS_PER_MESSAGE
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


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):g} MB"


def _validated_image(content: bytes, name: str) -> ValidatedFile | None:
    """The upload as an image when its header proves one, else None.

    Typed by its header, never the client's claim: a file that only starts like an
    image would reach the model as a block the API rejects on every later turn.
    """
    try:
        header = read_image_header(content)
    except MalformedImage:
        raise AttachmentRejected("attachment_invalid", "The image file is not readable.") from None
    if header is None:
        return None
    if len(content) > MAX_IMAGE_BYTES:
        raise AttachmentRejected(
            "attachment_too_large", f"Images can be at most {_mb(MAX_IMAGE_BYTES)}."
        )
    if max(header.width, header.height) > MAX_IMAGE_DIMENSION:
        raise AttachmentRejected(
            "attachment_too_large",
            f"Images can be at most {MAX_IMAGE_DIMENSION} pixels on each side.",
        )
    return ValidatedFile(content=content, media_type=header.media_type, filename=name)


def clean_filename(raw: str | None) -> str:
    # Control characters first, so one cannot hide a ".." from PurePath.
    raw = "".join(ch for ch in raw or "" if unicodedata.category(ch)[0] != "C")
    name = PurePath(raw.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        return "attachment"
    if len(name) > MAX_FILENAME_LENGTH:
        suffix = PurePath(name).suffix[:16]
        name = name[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name


def _base_media_type(claimed: str | None) -> str:
    return (claimed or "").split(";", 1)[0].strip().lower()


def validate_upload(content: bytes, *, filename: str | None, claimed_type: str | None):
    """Decide what an upload is, or raise ``AttachmentRejected``.

    A file with a text extension is text, whatever its first bytes. Otherwise images
    are recognised by their header alone; anything else must be one of the allowed
    text formats (by its claimed type when the name has no extension) and decode as
    UTF-8.
    """
    name = clean_filename(filename)
    if not content:
        raise AttachmentRejected("attachment_invalid", "The file is empty.")

    suffix = PurePath(name).suffix.lower()
    text_type = TEXT_MEDIA_TYPES_BY_EXTENSION.get(suffix)
    if text_type is None:
        image = _validated_image(content, name)
        if image is not None:
            return image

    claimed = _base_media_type(claimed_type)
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
            f"Text files can be at most {_mb(MAX_TEXT_BYTES)}.",
        )
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        raise AttachmentRejected(
            "attachment_invalid", "Text files must be UTF-8 encoded."
        ) from None
    return ValidatedFile(content=content, media_type=text_type, filename=name)


async def _existing_blob_id(sha256: str, size: int) -> int | None:
    return (
        await AttachmentBlob.objects.filter(sha256=sha256, size=size)
        .values_list("id", flat=True)
        .afirst()
    )


async def _blob_id_for(content: bytes) -> int:
    sha256 = hashlib.sha256(content).hexdigest()
    existing = await _existing_blob_id(sha256, len(content))
    if existing is not None:
        return existing
    try:
        blob = await AttachmentBlob.objects.acreate(
            sha256=sha256, size=len(content), content=content
        )
    except IntegrityError:
        # A concurrent upload of the same bytes created it first. By sha256 alone,
        # the unique key: the size can only differ on a SHA-256 collision.
        return (
            await AttachmentBlob.objects.filter(sha256=sha256).values_list("id", flat=True).aget()
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


# The base path must not start with "//": that is a protocol-relative URL to another host.
_ATTACHMENT_URL_RE = re.compile(
    r"(?:/(?!/)[A-Za-z0-9._~\-/]*)?/api/chat/attachments/"
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/"
)


def attachment_id_from_url(url) -> str | None:
    """The attachment id an attachment URL names, optionally behind a base path, else None."""
    if not isinstance(url, str):
        return None
    # fullmatch, not match with "$", which also accepts a trailing newline.
    match = _ATTACHMENT_URL_RE.fullmatch(url)
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
    try:
        # defer: the delete collector loads each row, and content can be megabytes.
        blobs_deleted, _ = await orphans.defer("content").adelete()
    except (IntegrityError, ProtectedError):
        # An upload reused a blob mid-sweep; one at a time, it spares only itself.
        blobs_deleted = await _acollect_orphans_one_by_one(orphans)
    return {"attachments": unsent_deleted, "blobs": blobs_deleted}


async def _acollect_orphans_one_by_one(orphans) -> int:
    orphan_ids = [blob_id async for blob_id in orphans.values_list("id", flat=True)]
    blobs_deleted = 0
    for blob_id in orphan_ids:
        try:
            deleted, _ = await orphans.filter(pk=blob_id).defer("content").adelete()
        except (IntegrityError, ProtectedError):
            logger.info("attachment GC: blob %s gained a reference mid-sweep", blob_id)
            continue
        blobs_deleted += deleted
    return blobs_deleted


# Where a HumanMessage carries its attachments; its content stays the typed text.
SCOUT_ATTACHMENTS_KWARG = "scout_attachments"


def message_attachments(message) -> list[dict]:
    """The ``{id, filename, media_type, size}`` entries a checkpointed message carries."""
    kwargs = getattr(message, "additional_kwargs", None) or {}
    entries = kwargs.get(SCOUT_ATTACHMENTS_KWARG)
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)]


def with_attachments(attachments) -> dict:
    """``additional_kwargs`` for a HumanMessage carrying ``attachments`` (none: empty)."""
    return {SCOUT_ATTACHMENTS_KWARG: list(attachments)} if attachments else {}


def merge_attachments(*groups) -> list[dict]:
    """The groups' attachments in order, each id once."""
    seen: set[str] = set()
    merged = []
    for group in groups:
        for entry in group:
            if entry["id"] not in seen:
                seen.add(entry["id"])
                merged.append(entry)
    return merged


def attachment_ids_from_parts(parts) -> tuple[list[str], str | None]:
    """The attachment ids a message's ``file`` parts name, in order, or an error message.

    Only this server's attachment URLs are accepted: a data: URL or any other link
    would put content the server never validated in front of the model.
    """
    too_many = f"A message can carry at most {MAX_ATTACHMENTS_PER_MESSAGE} attachments."
    ids: dict[str, None] = {}
    file_parts = 0
    for part in parts or []:
        if not isinstance(part, dict) or part.get("type") != "file":
            continue
        # Bounded as it goes: a body can hold tens of thousands of parts, and this
        # runs on the event loop.
        file_parts += 1
        if file_parts > MAX_FILE_PARTS:
            return [], too_many
        attachment_id = attachment_id_from_url(part.get("url"))
        if attachment_id is None:
            return [], "File parts must reference an uploaded attachment."
        ids[attachment_id] = None
        if len(ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            return [], too_many
    return list(ids), None


async def aresolve_for_send(ids: list[str], *, user, workspace, thread_id) -> list[dict] | None:
    """The attachments' metadata in ``ids`` order, or None unless every one is the
    user's, in ``workspace``, and unsent or already in this thread."""
    if not ids:
        return []
    rows = {
        str(row["id"]): row
        async for row in ChatAttachment.objects.filter(
            Q(thread__isnull=True) | Q(thread_id=thread_id),
            id__in=ids,
            user=user,
            workspace=workspace,
        ).values("id", "filename", "media_type", "blob__size")
    }
    if len(rows) != len(ids):
        return None
    return [
        {
            "id": attachment_id,
            "filename": rows[attachment_id]["filename"],
            "media_type": rows[attachment_id]["media_type"],
            "size": rows[attachment_id]["blob__size"],
        }
        for attachment_id in ids
    ]


async def abind_to_thread(ids, *, user, workspace, thread) -> list[str] | None:
    """Tie the attachments to the thread their message is sent in, so GC keeps them.

    Returns the ids this call bound (those still unsent), for ``aunbind`` should the
    message not be accepted after all; None, binding nothing, when one vanished
    since it was resolved (its unsent TTL ran out meanwhile).
    """
    ids = list(ids)
    if not ids:
        return []
    return await _bind(ids, user=user, workspace=workspace, thread=thread)


@sync_to_async
def _bind(ids: list[str], *, user, workspace, thread) -> list[str] | None:
    # One transaction with the rows locked, so a concurrent bind of the same ids
    # cannot be read as unsent here and then undone by this call's rollback.
    with transaction.atomic():
        rows = list(
            ChatAttachment.objects.select_for_update()
            .filter(Q(thread__isnull=True) | Q(thread=thread))
            .filter(id__in=ids, user=user, workspace=workspace)
            .values_list("id", "thread_id")
        )
        if len(rows) != len(ids):
            return None
        unsent = [str(attachment_id) for attachment_id, thread_id in rows if thread_id is None]
        ChatAttachment.objects.filter(id__in=unsent).update(thread=thread)
        return unsent


def uncarried(newly_bound, pending: dict) -> list[str]:
    """Of ``newly_bound``, the ids no part of the serialized held request carries.

    A part whose id was already held is not added again, so its attachments are not
    recorded; those must go back to unsent.
    """
    carried = {e["id"] for part in pending.get("parts", []) for e in part.get("attachments", [])}
    return [i for i in newly_bound if i not in carried]


async def amark_sent(ids, *, user, thread) -> None:
    """Record that a message carrying these is going to the agent, so its checkpoint
    may hold them and nothing may unbind them.

    Binds them too: a concurrent refused request may have unbound one meanwhile.
    """
    await _amark(ids, user=user, thread=thread, sent_at=timezone.now())


async def amark_held(ids, *, user, thread) -> None:
    """Record that a held request carries these, so no refused request unbinds them."""
    await _amark(ids, user=user, thread=thread, held_at=timezone.now())


async def _amark(ids, *, user, thread, **stamp) -> None:
    ids = list(ids)
    if ids:
        await ChatAttachment.objects.filter(id__in=ids, user=user).aupdate(thread=thread, **stamp)


async def aunbind(ids, *, thread) -> None:
    """Return attachments bound for a message that was never accepted to unsent, for GC.

    One an accepted message carries stays bound, whoever bound it.
    """
    if ids:
        await ChatAttachment.objects.filter(
            id__in=list(ids), thread=thread, sent_at__isnull=True, held_at__isnull=True
        ).aupdate(thread=None)


def unbind_unsent_sync(ids, *, thread_id) -> None:
    """``aunbind`` for sync callers, such as Django closing a response it never sent."""
    if ids:
        ChatAttachment.objects.filter(
            id__in=list(ids), thread_id=thread_id, sent_at__isnull=True, held_at__isnull=True
        ).update(thread=None)


def release_held_sync(ids, *, thread_id) -> None:
    """Release attachments a dropped held part bound, inside the caller's transaction.

    One a sent message also carries stays bound: its checkpoint may refer to it.
    """
    if ids:
        rows = ChatAttachment.objects.filter(id__in=list(ids), thread_id=thread_id)
        rows.update(held_at=None)
        rows.filter(sent_at__isnull=True).update(thread=None)
