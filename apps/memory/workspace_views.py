"""Workspace memory endpoints, nested under /api/workspaces/<workspace_id>/memory/.

Every member can list; READ_WRITE and MANAGE members can add; the author (while
they can still write) or a manager can edit or delete.
"""

from django.http import HttpResponse, JsonResponse

from apps.common.http import parse_json_object, string_field
from apps.common.utils import creator_display_name
from apps.knowledge.models import AgentLearning
from apps.memory.models import WorkspaceMemoryEvent
from apps.memory.services import MemoryValidationError
from apps.memory.workspace import (
    adelete_workspace_memory,
    asave_workspace_memory,
    aupdate_workspace_memory,
    can_add,
    can_change,
)
from apps.users.decorators import async_login_required
from apps.workspaces.access import access_denied_response, aresolve_workspace_access_ex
from apps.workspaces.models import WorkspaceRole

# New saves stop at MAX_WORKSPACE_MEMORIES; only legacy rows can exceed it, so
# this bounds the response for a workspace that predates the limit.
MAX_LISTED_MEMORIES = 200

_CHANGE_DENIED = "Only the memory's author or a workspace manager can change it."


def _serialize(memory: AgentLearning, user, role) -> dict:
    return {
        "id": str(memory.id),
        "content": memory.description,
        "tables": memory.applies_to_tables,
        "author_name": creator_display_name(memory.discovered_by_user)
        if memory.discovered_by_user_id
        else "Scout",
        "is_mine": memory.discovered_by_user_id == user.pk,
        "can_edit": can_change(role, memory, user),
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
    }


async def _access(user, workspace_id, minimum_role=WorkspaceRole.READ):
    result = await aresolve_workspace_access_ex(user, workspace_id, minimum_role=minimum_role)
    if not result.granted:
        return None, None, access_denied_response(result)
    return result.workspace, getattr(result.membership, "role", None), None


async def _body(request):
    body, err = parse_json_object(request)
    if err is not None:
        return None, None, err
    content, err = string_field(body or {}, "content")
    if err is not None:
        return None, None, err
    tables = (body or {}).get("tables")
    return content or "", tables, None


@async_login_required
async def workspace_memory_list_view(request, workspace_id):
    """GET lists the workspace's memories; POST adds one (READ_WRITE or MANAGE)."""
    user = request._authenticated_user

    if request.method == "GET":
        workspace, role, err = await _access(user, workspace_id)
        if err is not None:
            return err
        active = AgentLearning.objects.filter(workspace=workspace, is_active=True)
        rows = active.select_related("discovered_by_user").order_by("-created_at", "pk")[
            :MAX_LISTED_MEMORIES
        ]
        memories = [_serialize(m, user, role) async for m in rows]
        return JsonResponse(
            {"results": memories, "total": await active.acount(), "can_add": can_add(role)}
        )

    if request.method == "POST":
        workspace, role, err = await _access(user, workspace_id, WorkspaceRole.READ_WRITE)
        if err is not None:
            return err
        content, tables, err = await _body(request)
        if err is not None:
            return err
        try:
            result = await asave_workspace_memory(
                workspace,
                user,
                content,
                tables,
                source=WorkspaceMemoryEvent.Source.MEMORY_PAGE,
            )
        except MemoryValidationError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        memory = await AgentLearning.objects.select_related("discovered_by_user").aget(
            pk=result.memory.pk
        )
        return JsonResponse(_serialize(memory, user, role), status=201 if result.created else 200)

    return JsonResponse({"error": "Method not allowed"}, status=405)


@async_login_required
async def workspace_memory_detail_view(request, workspace_id, memory_id):
    """PATCH edits and DELETE removes a memory: its author while still a writer, or a manager."""
    user = request._authenticated_user
    if request.method not in {"PATCH", "DELETE"}:
        return JsonResponse({"error": "Method not allowed"}, status=405)

    workspace, role, err = await _access(user, workspace_id)
    if err is not None:
        return err
    memory = (
        await AgentLearning.objects.filter(id=memory_id, workspace=workspace, is_active=True)
        .select_related("discovered_by_user")
        .afirst()
    )
    if memory is None:
        return JsonResponse({"error": "Memory not found"}, status=404)
    if not can_change(role, memory, user):
        return JsonResponse({"error": _CHANGE_DENIED}, status=403)

    if request.method == "DELETE":
        await adelete_workspace_memory(memory, user)
        return HttpResponse(status=204)

    content, tables, err = await _body(request)
    if err is not None:
        return err
    try:
        memory = await aupdate_workspace_memory(memory, user, content, tables)
    except AgentLearning.DoesNotExist:
        return JsonResponse({"error": "Memory not found"}, status=404)
    except MemoryValidationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    memory = await AgentLearning.objects.select_related("discovered_by_user").aget(pk=memory.pk)
    return JsonResponse(_serialize(memory, user, role))
