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
from apps.projects.api.permissions import ProjectPermissionMixin

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


class KnowledgeListCreateView(ProjectPermissionMixin, APIView):
    """
    GET  /api/projects/{project_id}/knowledge/
    POST /api/projects/{project_id}/knowledge/
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

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

        if type_filter and type_filter in KNOWLEDGE_TYPES:
            types_to_query = [type_filter]
        else:
            types_to_query = list(KNOWLEDGE_TYPES.keys())

        all_items = []

        for type_name in types_to_query:
            type_config = KNOWLEDGE_TYPES[type_name]
            model = type_config["model"]
            serializer_class = type_config["serializer"]

            queryset = model.objects.filter(project=project)

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

        return Response({
            "results": paginated_items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_count": total_count,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1,
            },
        })

    def post(self, request, project_id):
        project = self.get_project(project_id)

        can_edit, error_response = self.check_edit_permission(request, project)
        if not can_edit:
            return error_response

        item_type = request.data.get("type")
        if not item_type or item_type not in KNOWLEDGE_TYPES:
            return Response(
                {"error": f"Invalid or missing type. Must be one of: {', '.join(KNOWLEDGE_TYPES.keys())}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        type_config = KNOWLEDGE_TYPES[item_type]
        serializer_class = type_config["serializer"]

        serializer = serializer_class(data=request.data, context={"request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        instance = serializer.save(project=project)

        if item_type == "entry":
            instance.created_by = request.user
            instance.save(update_fields=["created_by"])
        elif item_type == "learning":
            instance.discovered_by_user = request.user
            instance.save(update_fields=["discovered_by_user"])

        response_serializer = serializer_class(instance)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class KnowledgeDetailView(ProjectPermissionMixin, APIView):
    """
    GET    /api/projects/{project_id}/knowledge/{item_id}/
    PUT    /api/projects/{project_id}/knowledge/{item_id}/
    DELETE /api/projects/{project_id}/knowledge/{item_id}/
    """

    permission_classes = [IsAuthenticated]

    def _find_item(self, project, item_id):
        for type_name, type_config in KNOWLEDGE_TYPES.items():
            model = type_config["model"]
            try:
                item = model.objects.get(pk=item_id, project=project)
                return item, type_name, type_config["serializer"]
            except model.DoesNotExist:
                continue
        return None, None, None

    def get(self, request, project_id, item_id):
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        item, type_name, serializer_class = self._find_item(project, item_id)
        if not item:
            return Response(
                {"error": "Knowledge item not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = serializer_class(item)
        return Response(serializer.data)

    def put(self, request, project_id, item_id):
        project = self.get_project(project_id)

        can_edit, error_response = self.check_edit_permission(request, project)
        if not can_edit:
            return error_response

        item, type_name, serializer_class = self._find_item(project, item_id)
        if not item:
            return Response(
                {"error": "Knowledge item not found."},
                status=status.HTTP_404_NOT_FOUND,
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

    def delete(self, request, project_id, item_id):
        project = self.get_project(project_id)

        can_edit, error_response = self.check_edit_permission(request, project)
        if not can_edit:
            return error_response

        item, type_name, serializer_class = self._find_item(project, item_id)
        if not item:
            return Response(
                {"error": "Knowledge item not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class KnowledgeExportView(ProjectPermissionMixin, APIView):
    """
    GET /api/projects/{project_id}/knowledge/export/

    Export all KnowledgeEntry records as a zip of markdown files with YAML frontmatter.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        project = self.get_project(project_id)

        has_access, error_response = self.check_project_access(request, project)
        if not has_access:
            return error_response

        entries = KnowledgeEntry.objects.filter(project=project).order_by("title")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in entries:
                # Sanitize filename
                safe_title = "".join(
                    c if c.isalnum() or c in " -_" else "_" for c in entry.title
                ).strip()[:80]
                filename = f"{safe_title}.md"
                content = render_frontmatter(entry.title, entry.tags or [], entry.content)
                zf.writestr(filename, content)

        buf.seek(0)
        response = HttpResponse(buf.read(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="knowledge-{project.slug}.zip"'
        return response


class KnowledgeImportView(ProjectPermissionMixin, APIView):
    """
    POST /api/projects/{project_id}/knowledge/import/

    Import knowledge entries from a zip of markdown files with YAML frontmatter.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request, project_id):
        project = self.get_project(project_id)

        can_edit, error_response = self.check_edit_permission(request, project)
        if not can_edit:
            return error_response

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
                        project=project,
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
