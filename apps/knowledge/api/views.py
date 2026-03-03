"""API views for knowledge management."""

import io
import logging
import zipfile

from django.db.models import Q
from django.http import HttpResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.models import AgentLearning, KnowledgeEntry
from apps.knowledge.utils import parse_frontmatter, render_frontmatter

from .serializers import AgentLearningSerializer, KnowledgeEntrySerializer

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

KNOWLEDGE_TYPES = {
    "entry": {
        "model": KnowledgeEntry,
        "serializer": KnowledgeEntrySerializer,
        "search_fields": ["title", "content"],
    },
    "learning": {
        "model": AgentLearning,
        "serializer": AgentLearningSerializer,
        "search_fields": ["description", "original_error", "original_sql", "corrected_sql"],
    },
}


def _resolve_workspace(request):
    """Resolve the active TenantWorkspace for the authenticated user."""
    from django.db.models import F

    from apps.users.models import TenantMembership
    from apps.workspace.models import TenantWorkspace

    membership = (
        TenantMembership.objects.filter(user=request.user)
        .order_by(F("last_selected_at").desc(nulls_last=True))
        .first()
    )
    if not membership:
        return None, Response(
            {"error": "No tenant selected. Please select a domain first."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    workspace, _ = TenantWorkspace.objects.get_or_create(
        tenant_id=membership.tenant_id,
        defaults={"tenant_name": membership.tenant_name},
    )
    return workspace, None


def _resolve_custom_workspace(request):
    """Check for X-Custom-Workspace header and resolve the CustomWorkspace.

    Returns (custom_workspace, error_response). If the header is absent,
    returns (None, None) so callers can fall back to the tenant workspace path.
    """
    from apps.workspace.models import CustomWorkspace, WorkspaceMembership

    header_value = request.META.get("HTTP_X_CUSTOM_WORKSPACE")
    if not header_value:
        return None, None

    workspace = CustomWorkspace.objects.filter(id=header_value).first()
    if not workspace:
        return None, Response(
            {"error": "Custom workspace not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Verify the user is a member of this workspace
    if not WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).exists():
        return None, Response(
            {"error": "Not a member of this workspace."},
            status=status.HTTP_403_FORBIDDEN,
        )

    return workspace, None


def _get_custom_workspace_knowledge(custom_workspace, type_filter, search_query):
    """Aggregate knowledge entries from all member tenants plus workspace-specific entries.

    Each item dict is annotated with ``source`` and ``source_name`` fields.
    """
    if type_filter and type_filter in KNOWLEDGE_TYPES:
        types_to_query = [type_filter]
    else:
        types_to_query = list(KNOWLEDGE_TYPES.keys())

    # Build a mapping of member tenant workspace IDs → tenant names
    tenant_links = custom_workspace.custom_workspace_tenants.select_related("tenant_workspace")
    tenant_ws_map = {
        link.tenant_workspace_id: link.tenant_workspace.tenant_name for link in tenant_links
    }

    all_items = []

    for type_name in types_to_query:
        type_config = KNOWLEDGE_TYPES[type_name]
        model = type_config["model"]
        serializer_class = type_config["serializer"]

        # Entries from member tenant workspaces
        if tenant_ws_map:
            tenant_qs = model.objects.filter(workspace_id__in=tenant_ws_map.keys())
            if search_query:
                search_q = Q()
                for field in type_config["search_fields"]:
                    search_q |= Q(**{f"{field}__icontains": search_query})
                tenant_qs = tenant_qs.filter(search_q)

            serialized = serializer_class(tenant_qs, many=True).data
            for item in serialized:
                # Look up the workspace FK on the actual model instance
                item["source"] = "tenant"
                item["source_name"] = "Unknown"
            # Need to annotate per-item with the correct tenant name
            # Re-query with workspace_id to map correctly
            for obj in tenant_qs:
                ws_id = obj.workspace_id
                tenant_name = tenant_ws_map.get(ws_id, "Unknown")
                # Find the matching serialized item by id
                for item in serialized:
                    if str(item["id"]) == str(obj.pk):
                        item["source_name"] = tenant_name
                        break
            all_items.extend(serialized)

        # Entries directly on this custom workspace
        ws_qs = model.objects.filter(custom_workspace=custom_workspace)
        if search_query:
            search_q = Q()
            for field in type_config["search_fields"]:
                search_q |= Q(**{f"{field}__icontains": search_query})
            ws_qs = ws_qs.filter(search_q)

        ws_serialized = serializer_class(ws_qs, many=True).data
        for item in ws_serialized:
            item["source"] = "workspace"
            item["source_name"] = "This Workspace"
        all_items.extend(ws_serialized)

    return all_items


class KnowledgeListCreateView(APIView):
    """
    GET  /api/knowledge/
    POST /api/knowledge/
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Check for custom workspace mode first
        custom_workspace, cw_err = _resolve_custom_workspace(request)
        if cw_err:
            return cw_err

        type_filter = request.query_params.get("type")
        search_query = request.query_params.get("search", "").strip()

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (ValueError, TypeError):
            page = 1

        try:
            page_size = min(
                MAX_PAGE_SIZE,
                max(1, int(request.query_params.get("page_size", DEFAULT_PAGE_SIZE))),
            )
        except (ValueError, TypeError):
            page_size = DEFAULT_PAGE_SIZE

        if custom_workspace:
            all_items = _get_custom_workspace_knowledge(
                custom_workspace, type_filter, search_query
            )
        else:
            workspace, err = _resolve_workspace(request)
            if err:
                return err

            if type_filter and type_filter in KNOWLEDGE_TYPES:
                types_to_query = [type_filter]
            else:
                types_to_query = list(KNOWLEDGE_TYPES.keys())

            all_items = []

            for type_name in types_to_query:
                type_config = KNOWLEDGE_TYPES[type_name]
                model = type_config["model"]
                serializer_class = type_config["serializer"]

                queryset = model.objects.filter(workspace=workspace)

                if search_query:
                    search_q = Q()
                    for field in type_config["search_fields"]:
                        search_q |= Q(**{f"{field}__icontains": search_query})
                    queryset = queryset.filter(search_q)

                serializer = serializer_class(queryset, many=True)
                all_items.extend(serializer.data)

        all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        total_count = len(all_items)
        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        paginated_items = all_items[start_index:end_index]

        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

        return Response(
            {
                "results": paginated_items,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    "has_next": page < total_pages,
                    "has_previous": page > 1,
                },
            }
        )

    def post(self, request):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        item_type = request.data.get("type")
        if not item_type or item_type not in KNOWLEDGE_TYPES:
            return Response(
                {
                    "error": f"Invalid or missing type. Must be one of: {', '.join(KNOWLEDGE_TYPES.keys())}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        type_config = KNOWLEDGE_TYPES[item_type]
        serializer_class = type_config["serializer"]

        serializer = serializer_class(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        instance = serializer.save(workspace=workspace)

        if item_type == "entry":
            instance.created_by = request.user
            instance.save(update_fields=["created_by"])
        elif item_type == "learning":
            instance.discovered_by_user = request.user
            instance.save(update_fields=["discovered_by_user"])

        response_serializer = serializer_class(instance)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class KnowledgeDetailView(APIView):
    """
    GET    /api/knowledge/<item_id>/
    PUT    /api/knowledge/<item_id>/
    DELETE /api/knowledge/<item_id>/
    """

    permission_classes = [IsAuthenticated]

    def _find_item(self, workspace, item_id):
        for type_name, type_config in KNOWLEDGE_TYPES.items():
            model = type_config["model"]
            try:
                item = model.objects.get(pk=item_id, workspace=workspace)
                return item, type_name, type_config["serializer"]
            except model.DoesNotExist:
                continue
        return None, None, None

    def get(self, request, item_id):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        item, type_name, serializer_class = self._find_item(workspace, item_id)
        if not item:
            return Response(
                {"error": "Knowledge item not found."}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = serializer_class(item)
        return Response(serializer.data)

    def put(self, request, item_id):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        item, type_name, serializer_class = self._find_item(workspace, item_id)
        if not item:
            return Response(
                {"error": "Knowledge item not found."}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = serializer_class(
            item,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()
        return Response(serializer.data)

    def delete(self, request, item_id):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        item, type_name, serializer_class = self._find_item(workspace, item_id)
        if not item:
            return Response(
                {"error": "Knowledge item not found."}, status=status.HTTP_404_NOT_FOUND
            )

        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class KnowledgeExportView(APIView):
    """
    GET /api/knowledge/export/

    Export all KnowledgeEntry records as a zip of markdown files with YAML frontmatter.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        entries = KnowledgeEntry.objects.filter(workspace=workspace).order_by("title")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in entries:
                safe_title = "".join(
                    c if c.isalnum() or c in " -_" else "_" for c in entry.title
                ).strip()[:80]
                filename = f"{safe_title}.md"
                content = render_frontmatter(entry.title, entry.tags or [], entry.content)
                zf.writestr(filename, content)

        buf.seek(0)
        safe_name = workspace.tenant_id.replace("/", "_")
        response = HttpResponse(buf.read(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="knowledge-{safe_name}.zip"'
        return response


class KnowledgeImportView(APIView):
    """
    POST /api/knowledge/import/

    Import knowledge entries from a zip of markdown files with YAML frontmatter.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        workspace, err = _resolve_workspace(request)
        if err:
            return err

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response(
                {"error": "No file uploaded. Send a zip file as 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with zipfile.ZipFile(uploaded) as zf:
                created = 0
                updated = 0
                for name in zf.namelist():
                    if not name.endswith(".md"):
                        continue
                    raw = zf.read(name).decode("utf-8")
                    title, tags, body = parse_frontmatter(raw)
                    if not title:
                        continue

                    _, was_created = KnowledgeEntry.objects.update_or_create(
                        workspace=workspace,
                        title=title,
                        defaults={
                            "content": body,
                            "tags": tags,
                            "created_by": request.user,
                        },
                    )
                    if was_created:
                        created += 1
                    else:
                        updated += 1

        except zipfile.BadZipFile:
            return Response(
                {"error": "Invalid zip file."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {"created": created, "updated": updated},
            status=status.HTTP_200_OK,
        )
