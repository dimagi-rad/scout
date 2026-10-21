"""Chat attachment upload, download, dedup and garbage collection (#913)."""

import hashlib
import uuid
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError, QuerySet
from django.test import AsyncClient
from django.utils import timezone

from apps.chat import attachments
from apps.chat.attachments import (
    AttachmentRejected,
    acollect_garbage,
    attachment_id_from_url,
    validate_upload,
)
from apps.chat.models import AttachmentBlob, ChatAttachment, Thread
from apps.chat.rate_limiting import check_and_record
from apps.workspaces.models import WorkspaceMembership
from tests import image_fixtures as images

PNG = images.png()

UPLOAD_URL = "/api/chat/attachments/"


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    cache.clear()
    yield
    cache.clear()


async def _client(email, password="testpass123", *, csrf=True):
    client = AsyncClient(enforce_csrf_checks=True)
    await client.alogin(email=email, password=password)
    client.csrf_headers = {}
    if csrf:
        token = (await client.get("/api/auth/csrf/")).json()["csrfToken"]
        client.csrf_headers = {"X-CSRFToken": token}
    return client


async def _upload(client, workspace, content, name, content_type):
    return await client.post(
        UPLOAD_URL,
        data={
            "file": SimpleUploadedFile(name, content, content_type=content_type),
            "workspaceId": str(workspace.id),
        },
        headers=getattr(client, "csrf_headers", {}),
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (images.png(), "image/png"),
        (images.jpeg(), "image/jpeg"),
        (images.gif(), "image/gif"),
        (images.webp_vp8x(), "image/webp"),
    ],
    ids=["png", "jpeg", "gif", "webp"],
)
def test_validate_types_images_by_header(content, expected):
    assert validate_upload(content, filename="shot", claimed_type="").media_type == expected


def test_validate_prefers_text_when_the_extension_says_text():
    validated = validate_upload(b"GIF89a is a format", filename="notes.txt", claimed_type="")
    assert validated.media_type == "text/plain"


def test_validate_rejects_a_text_file_with_jpeg_magic_as_text():
    with pytest.raises(AttachmentRejected) as excinfo:
        validate_upload(b"\xff\xd8\xff<html>garbage", filename="a.txt", claimed_type="")
    assert excinfo.value.reason == "attachment_invalid"
    assert "UTF-8" in excinfo.value.message


def test_validate_rejects_an_image_whose_header_is_malformed():
    with pytest.raises(AttachmentRejected) as excinfo:
        validate_upload(b"\xff\xd8\xff<html>garbage", filename="a.jpg", claimed_type="")
    assert excinfo.value.reason == "attachment_invalid"


def test_validate_rejects_images_over_the_api_dimension_limit():
    validate_upload(images.png(8000, 10), filename="wide.png", claimed_type="")
    with pytest.raises(AttachmentRejected) as excinfo:
        validate_upload(images.png(8001, 10), filename="wide.png", claimed_type="")
    assert excinfo.value.reason == "attachment_too_large"


def test_validate_ignores_claimed_image_type():
    validated = validate_upload(PNG, filename="shot.jpg", claimed_type="image/jpeg")
    assert validated.media_type == "image/png"


def test_validate_text_by_extension():
    validated = validate_upload(b'{"a": 1}', filename="registry.json", claimed_type="text/plain")
    assert validated.media_type == "application/json"


def test_validate_extensionless_text_uses_claimed_type():
    validated = validate_upload(b"a,b\n1,2\n", filename="export", claimed_type="text/csv")
    assert validated.media_type == "text/csv"


@pytest.mark.parametrize(
    ("content", "filename", "claimed", "reason"),
    [
        (b"<svg/>", "logo.svg", "image/svg+xml", "attachment_type"),
        (b"MZ\x90\x00", "tool.exe", "application/octet-stream", "attachment_type"),
        (b"<html></html>", "page.html", "text/html", "attachment_type"),
        (b"not an image", "shot.png", "image/png", "attachment_invalid"),
        (b"\xff\xfe\x00bad", "data.csv", "text/csv", "attachment_invalid"),
        (b"", "empty.txt", "text/plain", "attachment_invalid"),
        (b"x" * (attachments.MAX_TEXT_BYTES + 1), "big.txt", "text/plain", "attachment_too_large"),
        (
            PNG + b"\x00" * attachments.MAX_IMAGE_BYTES,
            "big.png",
            "image/png",
            "attachment_too_large",
        ),
    ],
    # By filename: pytest would otherwise spell the multi-megabyte byte strings into
    # the test ids, and CI's log streaming stalls on 20 MB lines.
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_validate_rejects(content, filename, claimed, reason):
    with pytest.raises(AttachmentRejected) as excinfo:
        validate_upload(content, filename=filename, claimed_type=claimed)
    assert excinfo.value.reason == reason


def test_validate_strips_path_and_control_characters():
    validated = validate_upload(b"hi", filename="../../etc/pa\x00ss\nwd.txt", claimed_type="")
    assert validated.filename == "passwd.txt"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/api/chat/attachments/0b6f7a5e-1c1d-4a7e-9d43-3f1f2b0c9a11/", True),
        ("/scout/api/chat/attachments/0b6f7a5e-1c1d-4a7e-9d43-3f1f2b0c9a11/", True),
        ("data:image/png;base64,AAAA", False),
        ("https://evil.example/api/chat/attachments/0b6f7a5e-1c1d-4a7e-9d43-3f1f2b0c9a11/", False),
        ("//evil.example/api/chat/attachments/0b6f7a5e-1c1d-4a7e-9d43-3f1f2b0c9a11/", False),
        ("/api/chat/attachments/not-a-uuid/", False),
        ("/api/chat/attachments/0b6f7a5e-1c1d-4a7e-9d43-3f1f2b0c9a11/\n", False),
        ("/api/chat/attachments/0b6f7a5e-1c1d-4a7e-9d43-3f1f2b0c9a11/../x", False),
        (None, False),
    ],
)
def test_attachment_id_from_url(url, expected):
    result = attachment_id_from_url(url)
    if expected:
        assert result == "0b6f7a5e-1c1d-4a7e-9d43-3f1f2b0c9a11"
    else:
        assert result is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_returns_attachment(user, workspace):
    client = await _client(user.email)
    resp = await _upload(client, workspace, PNG, "shot.png", "image/png")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    attachment = await ChatAttachment.objects.aget(id=body["id"])
    assert body == {
        "id": str(attachment.id),
        "filename": "shot.png",
        "mediaType": "image/png",
        "size": len(PNG),
        "url": f"/api/chat/attachments/{attachment.id}/",
    }
    assert attachment.thread_id is None
    assert attachment.workspace_id == workspace.id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_identical_uploads_share_one_blob(user, workspace):
    client = await _client(user.email)
    first = await _upload(client, workspace, PNG, "a.png", "image/png")
    second = await _upload(client, workspace, PNG, "b.png", "image/png")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert await AttachmentBlob.objects.acount() == 1
    assert await ChatAttachment.objects.acount() == 2
    assert {a.filename async for a in ChatAttachment.objects.all()} == {"a.png", "b.png"}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_rejects_with_reason(user, workspace):
    client = await _client(user.email)
    resp = await _upload(client, workspace, b"<svg/>", "x.svg", "image/svg+xml")
    assert resp.status_code == 400
    assert resp.json()["reason"] == "attachment_type"
    assert await ChatAttachment.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_requires_file(user, workspace):
    client = await _client(user.email)
    resp = await client.post(
        UPLOAD_URL, data={"workspaceId": str(workspace.id)}, headers=client.csrf_headers
    )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "attachment_invalid"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_requires_workspace_access(user, workspace, other_user):
    client = await _client(other_user.email, "otherpass123")
    resp = await _upload(client, workspace, PNG, "shot.png", "image/png")
    assert resp.status_code in (403, 404)
    assert await ChatAttachment.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_requires_login(workspace):
    client = AsyncClient()
    resp = await _upload(client, workspace, PNG, "shot.png", "image/png")
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_requires_csrf(user, workspace):
    client = await _client(user.email, csrf=False)
    resp = await _upload(client, workspace, PNG, "shot.png", "image/png")
    assert resp.status_code == 403
    assert await ChatAttachment.objects.acount() == 0


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_caps_unsent_attachments(user, workspace, monkeypatch):
    monkeypatch.setattr("apps.chat.attachment_views.MAX_UNSENT_PER_USER", 1)
    client = await _client(user.email)
    assert (await _upload(client, workspace, PNG, "a.png", "image/png")).status_code == 201
    resp = await _upload(client, workspace, PNG, "b.png", "image/png")
    assert resp.status_code == 429
    assert resp.json()["reason"] == "attachment_limit"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_owner_downloads_image_inline(user, workspace):
    client = await _client(user.email)
    url = (await _upload(client, workspace, PNG, "shot.png", "image/png")).json()["url"]
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp["Content-Type"] == "image/png"
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["Content-Disposition"].startswith("inline")
    assert resp["Cache-Control"] == "private, max-age=300"
    assert resp["Content-Security-Policy"] == "default-src 'none'; sandbox"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_owner_downloads_text_as_attachment(user, workspace):
    client = await _client(user.email)
    url = (
        await _upload(client, workspace, b'{"a": 1}', "registry.json", "application/json")
    ).json()["url"]
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.content == b'{"a": 1}'
    assert resp["Content-Type"] == "application/json; charset=utf-8"
    assert resp["Content-Disposition"].startswith("attachment")
    assert 'filename="registry.json"' in resp["Content-Disposition"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_other_user_cannot_download(user, workspace, other_user):
    owner = await _client(user.email)
    url = (await _upload(owner, workspace, PNG, "shot.png", "image/png")).json()["url"]
    # The other user holds their own upload of the same bytes: dedup must not leak.
    await ChatAttachment.objects.acreate(
        blob=await AttachmentBlob.objects.aget(),
        user=other_user,
        workspace=workspace,
        filename="mine.png",
        media_type="image/png",
    )
    intruder = await _client(other_user.email, "otherpass123")
    resp = await intruder.get(url)
    assert resp.status_code == 404
    assert (await intruder.get(f"/api/chat/attachments/{uuid.uuid4()}/")).status_code == 404


def _attachment(user, workspace, blob, *, thread=None, age=timedelta(0)):
    attachment = ChatAttachment.objects.create(
        blob=blob,
        user=user,
        workspace=workspace,
        thread=thread,
        filename="f.txt",
        media_type="text/plain",
    )
    ChatAttachment.objects.filter(pk=attachment.pk).update(created_at=timezone.now() - age)
    return attachment


def _blob(content, *, age=timedelta(days=2)):
    blob = AttachmentBlob.objects.create(
        sha256=content.hex().ljust(64, "0")[:64], size=len(content), content=content
    )
    AttachmentBlob.objects.filter(pk=blob.pk).update(created_at=timezone.now() - age)
    return blob


@pytest.fixture
def gc_rows(user, workspace):
    thread = Thread.objects.create(user=user, workspace=workspace)
    sent_blob = _blob(b"sent")
    stale_blob = _blob(b"stale")
    shared_blob = _blob(b"shared")
    orphan_blob = _blob(b"orphan")
    fresh_orphan_blob = _blob(b"fresh", age=timedelta(minutes=5))
    return {
        "sent": _attachment(user, workspace, sent_blob, thread=thread, age=timedelta(days=3)),
        "stale": _attachment(user, workspace, stale_blob, age=timedelta(hours=25)),
        "recent": _attachment(user, workspace, shared_blob, age=timedelta(hours=1)),
        "stale_shared": _attachment(user, workspace, shared_blob, age=timedelta(hours=30)),
        "blobs": {
            "sent": sent_blob,
            "stale": stale_blob,
            "shared": shared_blob,
            "orphan": orphan_blob,
            "fresh_orphan": fresh_orphan_blob,
        },
        "thread": thread,
    }


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_gc_deletes_stale_unsent_and_orphan_blobs(gc_rows):
    result = await acollect_garbage()
    remaining = {a.pk async for a in ChatAttachment.objects.all()}
    assert remaining == {gc_rows["sent"].pk, gc_rows["recent"].pk}
    blobs = {b.pk async for b in AttachmentBlob.objects.all()}
    expected = {gc_rows["blobs"][k].pk for k in ("sent", "shared", "fresh_orphan")}
    assert blobs == expected
    assert result == {"attachments": 2, "blobs": 2}


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_deleting_thread_cascades_attachments(gc_rows):
    await Thread.objects.filter(pk=gc_rows["thread"].pk).adelete()
    assert not await ChatAttachment.objects.filter(pk=gc_rows["sent"].pk).aexists()
    await acollect_garbage()
    assert not await AttachmentBlob.objects.filter(pk=gc_rows["blobs"]["sent"].pk).aexists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_reuses_blob_and_survives_concurrent_gc(user, workspace, monkeypatch):
    blob = await AttachmentBlob.objects.acreate(
        sha256=hashlib.sha256(PNG).hexdigest(), size=len(PNG), content=PNG
    )
    real_blob_id_for = attachments._blob_id_for
    calls = []

    async def gc_after_lookup(content):
        blob_id = await real_blob_id_for(content)
        if not calls:
            await AttachmentBlob.objects.filter(pk=blob.pk).adelete()
        calls.append(blob_id)
        return blob_id

    monkeypatch.setattr(attachments, "_blob_id_for", gc_after_lookup)
    validated = validate_upload(PNG, filename="shot.png", claimed_type="image/png")
    attachment = await attachments.astore_attachment(validated, user=user, workspace=workspace)
    assert calls[0] == blob.pk
    assert attachment.blob_id == calls[1] != blob.pk
    assert await AttachmentBlob.objects.acount() == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_rate_limit_is_separate_from_chat(user, workspace, settings):
    settings.ATTACHMENT_UPLOAD_RATE_LIMIT = 1
    client = await _client(user.email)
    assert (await _upload(client, workspace, PNG, "a.png", "image/png")).status_code == 201
    resp = await _upload(client, workspace, PNG, "b.png", "image/png")
    assert resp.status_code == 429
    assert resp.json()["reason"] == "attachment_rate_limited"
    is_limited, _ = await check_and_record(user.pk)
    assert not is_limited


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_gc_skips_a_blob_that_gains_a_reference_and_collects_the_rest(gc_rows, monkeypatch):
    real_adelete = QuerySet.adelete
    contended = gc_rows["blobs"]["orphan"].pk
    raised = []

    async def adelete(queryset):
        pks = [pk async for pk in queryset.values_list("pk", flat=True)]
        if queryset.model is AttachmentBlob and contended in pks:
            raised.append(len(pks))
            raise ProtectedError("referenced", set())
        return await real_adelete(queryset)

    monkeypatch.setattr(QuerySet, "adelete", adelete)
    result = await acollect_garbage()
    # The bulk delete fails on the contended blob; the fallback spares only it.
    assert raised == [2, 1]
    assert await AttachmentBlob.objects.filter(pk=contended).aexists()
    assert not await AttachmentBlob.objects.filter(pk=gc_rows["blobs"]["stale"].pk).aexists()
    assert result["blobs"] == 1


def test_validate_does_not_let_control_characters_hide_a_parent_dir():
    assert validate_upload(b"hi", filename="..\x00", claimed_type="text/plain").filename == (
        "attachment"
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upload_rejects_a_malformed_workspace_id(user):
    client = await _client(user.email)
    resp = await client.post(
        UPLOAD_URL,
        data={"file": SimpleUploadedFile("a.png", PNG), "workspaceId": "nope"},
        headers=client.csrf_headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_download_needs_current_workspace_access(user, workspace):
    client = await _client(user.email)
    url = (await _upload(client, workspace, PNG, "shot.png", "image/png")).json()["url"]
    await WorkspaceMembership.objects.filter(user=user, workspace=workspace).adelete()
    assert (await client.get(url)).status_code in (403, 404)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_download_header_survives_unicode_and_crlf_in_the_filename(user, workspace):
    raw_name = "r\u00e9sum\u00e9\r\nX-Evil: 1.csv"
    assert attachments.clean_filename(raw_name) == "r\u00e9sum\u00e9X-Evil: 1.csv"
    blob = await AttachmentBlob.objects.acreate(sha256="c" * 64, size=3, content=b"a,b")
    stored = await ChatAttachment.objects.acreate(
        blob=blob,
        user=user,
        workspace=workspace,
        filename=attachments.clean_filename(raw_name),
        media_type="text/csv",
    )
    client = await _client(user.email)
    resp = await client.get(f"/api/chat/attachments/{stored.id}/")
    disposition = resp["Content-Disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert disposition == "attachment; filename*=utf-8''r%C3%A9sum%C3%A9X-Evil%3A%201.csv"
    assert "X-Evil" not in dict(resp.items())
    assert resp["Content-Security-Policy"] == "default-src 'none'; sandbox"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_concurrent_identical_upload_reuses_the_winners_blob(monkeypatch):
    content = images.png(5, 5)
    winner = await AttachmentBlob.objects.acreate(
        sha256=hashlib.sha256(content).hexdigest(), size=len(content), content=content
    )

    async def lost_the_race(sha256, size):
        # The other upload inserted between this one's lookup and its insert.
        return None

    monkeypatch.setattr(attachments, "_existing_blob_id", lost_the_race)
    assert await attachments._blob_id_for(content) == winner.pk
    assert await AttachmentBlob.objects.acount() == 1
