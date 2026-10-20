"""Upload and download endpoints for chat attachments.

Uploads are multipart, not JSON: a 5 MB image would exceed Django's 2.5 MB
in-memory JSON body limit, while uploaded files stream to disk.
"""

from __future__ import annotations

import uuid

from django.http import HttpResponse, JsonResponse
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import csrf_protect

from apps.chat.attachments import (
    MAX_IMAGE_BYTES,
    MAX_UNSENT_PER_USER,
    AttachmentRejected,
    acount_unsent,
    astore_attachment,
    attachment_payload,
    validate_upload,
)
from apps.chat.helpers import _resolve_chat_access, async_login_required
from apps.chat.models import ChatAttachment
from apps.chat.rate_limiting import attachment_upload_rate_limit
from apps.workspaces.access import access_denied_response


def _rejected(reason: str, message: str) -> JsonResponse:
    return JsonResponse({"error": message, "reason": reason}, status=400)


@csrf_protect
@async_login_required
@attachment_upload_rate_limit
async def attachment_upload_view(request):
    """POST /api/chat/attachments/ — multipart ``file`` + ``workspaceId``."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    user = request._authenticated_user

    workspace_id = request.POST.get("workspaceId")
    if not workspace_id:
        return JsonResponse({"error": "workspaceId is required"}, status=400)
    upload = request.FILES.get("file")
    if upload is None:
        return _rejected("attachment_invalid", "No file was uploaded.")

    access, tm, is_multi_tenant = await _resolve_chat_access(user, workspace_id)
    workspace = access.workspace
    if workspace is None:
        return access_denied_response(access, reason="access_denied")
    if tm is None and not is_multi_tenant:
        return JsonResponse(
            {"error": "No tenant membership for this workspace", "reason": "access_denied"},
            status=403,
        )

    # Checked before reading, so an oversized upload is never pulled into memory.
    if upload.size > MAX_IMAGE_BYTES:
        return _rejected(
            "attachment_too_large",
            f"Attachments can be at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
        )
    if await acount_unsent(user) >= MAX_UNSENT_PER_USER:
        return JsonResponse(
            {
                "error": "Too many unsent attachments. Send or remove some and try again.",
                "reason": "attachment_limit",
            },
            status=429,
        )

    try:
        validated = validate_upload(
            upload.read(), filename=upload.name, claimed_type=upload.content_type
        )
    except AttachmentRejected as e:
        return _rejected(e.reason, e.message)

    attachment = await astore_attachment(validated, user=user, workspace=workspace)
    return JsonResponse(attachment_payload(attachment, len(validated.content)), status=201)


@async_login_required
async def attachment_detail_view(request, attachment_id: uuid.UUID):
    """GET /api/chat/attachments/<id>/ — the bytes, to the uploader only (404 otherwise)."""
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    user = request._authenticated_user
    attachment = (
        await ChatAttachment.objects.select_related("blob")
        .filter(id=attachment_id, user=user)
        .afirst()
    )
    if attachment is None:
        return JsonResponse({"error": "Attachment not found"}, status=404)

    is_image = attachment.media_type.startswith("image/")
    content_type = attachment.media_type if is_image else f"{attachment.media_type}; charset=utf-8"
    response = HttpResponse(bytes(attachment.blob.content), content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=not is_image, filename=attachment.filename
    )
    response["Cache-Control"] = "private, max-age=86400"
    return response
