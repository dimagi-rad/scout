"""Personal memory endpoints: each user reads and edits only their own rows."""

from django.http import HttpResponse, JsonResponse

from apps.common.http import parse_json_object, string_field
from apps.memory.models import PersonalMemory
from apps.memory.services import (
    MAX_PERSONAL_MEMORIES,
    MemoryValidationError,
    asave_personal_memory,
    aupdate_personal_memory,
)
from apps.users.decorators import async_login_required


def _serialize(memory: PersonalMemory) -> dict:
    return {
        "id": str(memory.id),
        "content": memory.content,
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
    }


@async_login_required
async def personal_memory_list_view(request):
    """GET lists the caller's memories; POST adds one."""
    user = request._authenticated_user
    if request.method == "GET":
        memories = [_serialize(m) async for m in PersonalMemory.objects.filter(user=user)]
        return JsonResponse({"results": memories, "limit": MAX_PERSONAL_MEMORIES})

    if request.method == "POST":
        body, err = parse_json_object(request)
        if err is not None:
            return err
        content, err = string_field(body or {}, "content")
        if err is not None:
            return err
        try:
            result = await asave_personal_memory(user, content or "")
        except MemoryValidationError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse(_serialize(result.memory), status=201 if result.created else 200)

    return JsonResponse({"error": "Method not allowed"}, status=405)


@async_login_required
async def personal_memory_detail_view(request, memory_id):
    """PATCH edits and DELETE removes one of the caller's memories.

    Another user's id is a 404, not a 403, so ids don't leak whose they are.
    """
    user = request._authenticated_user
    memory = await PersonalMemory.objects.filter(id=memory_id, user=user).afirst()
    if memory is None:
        return JsonResponse({"error": "Memory not found"}, status=404)

    if request.method == "PATCH":
        body, err = parse_json_object(request)
        if err is not None:
            return err
        content, err = string_field(body or {}, "content")
        if err is not None:
            return err
        try:
            updated = await aupdate_personal_memory(memory, user, content or "")
        except MemoryValidationError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if updated is None:
            return JsonResponse({"error": "Memory not found"}, status=404)
        return JsonResponse(_serialize(updated))

    if request.method == "DELETE":
        await memory.adelete()
        return HttpResponse(status=204)

    return JsonResponse({"error": "Method not allowed"}, status=405)
