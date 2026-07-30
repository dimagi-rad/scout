"""API views for knowledge management."""

import io
import logging
import zipfile

import yaml
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.knowledge.models import AgentLearning, KnowledgeEntry
from apps.knowledge.utils import parse_frontmatter, render_frontmatter
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

from .serializers import AgentLearningSerializer, KnowledgeEntrySerializer

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Cap decompressed import size to prevent a zip bomb inflating the knowledge
# base, which is re-billed into the system prompt on every LLM call (arch #254, 01#4).
MAX_IMPORT_DECOMPRESSED_BYTES = 25 * 1024 * 1024

KNOWLEDGE_TYPES = {
    "entry": {
        "model": KnowledgeEntry,
        "serializer": KnowledgeEntrySerializer,
        "search_fields": ["title", "content"],
        # Join created_by so the page slice doesn't N+1 (arch #254, 05#7).
        "select_related": ["created_by"],
    },
    "learning": {
        "model": AgentLearning,
        "serializer": AgentLearningSerializer,
        "search_fields": ["description", "original_error", "original_sql", "corrected_sql"],
        "select_related": [],
    },
}


class KnowledgeListCreateView(APIView):
    """
    GET  /api/knowledge/
    POST /api/knowledge/
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

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

        start_index = (page - 1) * page_size
        end_index = start_index + page_size

        # Paginate in the DB, not in Python (arch #254, finding 05#7). The list
        # merges two models by created_at desc; fetch only up to end_index per
        # model, merge, then slice — so serialization is bounded by page*page_size
        # per type rather than the full table.
        total_count = 0
        candidates = []
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

            total_count += queryset.count()

            page_window = queryset.order_by("-created_at")
            select_related = type_config.get("select_related")
            if select_related:
                page_window = page_window.select_related(*select_related)
            page_window = page_window[:end_index]
            serializer = serializer_class(page_window, many=True)
            candidates.extend(serializer.data)

        candidates.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        paginated_items = candidates[start_index:end_index]

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

    def post(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        item_type = request.data.get("type")
        if item_type == "learning":
            return Response(
                {
                    "error": "AgentLearning entries are created automatically by the agent and cannot be manually created."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
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

    def get(self, request, workspace_id, item_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        item, _type_name, serializer_class = self._find_item(workspace, item_id)
        if not item:
            return Response(
                {"error": "Knowledge item not found."}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = serializer_class(item)
        return Response(serializer.data)

    def put(self, request, workspace_id, item_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        item, _type_name, serializer_class = self._find_item(workspace, item_id)
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

    def delete(self, request, workspace_id, item_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        item, _type_name, _serializer_class = self._find_item(workspace, item_id)
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

    def get(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        entries = KnowledgeEntry.objects.filter(workspace=workspace).order_by("title", "created_at")

        buf = io.BytesIO()
        used_filenames: set[str] = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in entries:
                safe_title = "".join(
                    c if c.isalnum() or c in " -_" else "_" for c in entry.title
                ).strip()[:80]
                if not safe_title:
                    safe_title = "untitled"
                # Disambiguate duplicate titles so distinct entries don't collapse
                # onto one zip member and lose data on round trip (arch #262, finding 05#8).
                filename = f"{safe_title}.md"
                suffix = 2
                while filename in used_filenames:
                    filename = f"{safe_title}_{suffix}.md"
                    suffix += 1
                used_filenames.add(filename)
                content = render_frontmatter(entry.title, entry.tags or [], entry.content)
                zf.writestr(filename, content)

        buf.seek(0)
        tenant = workspace.tenant
        safe_name = (tenant.external_id if tenant else str(workspace.id)).replace("/", "_")
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

    def post(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(request, workspace_id)
        if err:
            return err

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response(
                {"error": "No file uploaded. Send a zip file as 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            zf = zipfile.ZipFile(uploaded)
        except zipfile.BadZipFile:
            return Response(
                {"error": "Invalid zip file."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Reject zip bombs before reading any member, using the directory's
        # advertised uncompressed sizes (arch #254, 01#4).
        total_uncompressed = sum(info.file_size for info in zf.infolist())
        if total_uncompressed > MAX_IMPORT_DECOMPRESSED_BYTES:
            return Response(
                {
                    "error": (
                        "Import archive is too large when decompressed "
                        f"({total_uncompressed} bytes; limit "
                        f"{MAX_IMPORT_DECOMPRESSED_BYTES})."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = 0
        updated = 0
        skipped = 0
        errors: list[dict] = []

        # Pool of pre-existing PKs per title; each consumed at most once so a
        # re-import updates in place (idempotent) while N duplicate titles map to
        # N entries instead of collapsing onto one (arch #262, finding 05#8).
        available_by_title: dict[str, list] = {}
        for pk, title in KnowledgeEntry.objects.filter(workspace=workspace).values_list(
            "id", "title"
        ):
            available_by_title.setdefault(title, []).append(pk)

        # Atomic: a hard DB failure rolls back the whole batch (arch #262, finding
        # 05#8). Per-entry parse/decode errors are collected, not fatal.
        try:
            with zf, transaction.atomic():
                for name in zf.namelist():
                    if not name.endswith(".md"):
                        continue
                    try:
                        raw = zf.read(name).decode("utf-8")
                    except UnicodeDecodeError:
                        errors.append({"file": name, "error": "File is not valid UTF-8."})
                        continue
                    try:
                        title, tags, body = parse_frontmatter(raw)
                    except (ValueError, yaml.YAMLError) as exc:
                        errors.append({"file": name, "error": f"Malformed frontmatter: {exc}"})
                        continue

                    if not title:
                        skipped += 1
                        continue

                    pool = available_by_title.get(title)
                    if pool:
                        pk = pool.pop(0)
                        KnowledgeEntry.objects.filter(pk=pk).update(content=body, tags=tags)
                        updated += 1
                    else:
                        KnowledgeEntry.objects.create(
                            workspace=workspace,
                            title=title,
                            content=body,
                            tags=tags,
                            created_by=request.user,
                        )
                        created += 1
        except Exception:
            logger.exception("Knowledge import failed for workspace %s", workspace.id)
            return Response(
                {"error": "Import failed; no entries were imported.", "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = {"created": created, "updated": updated, "skipped": skipped, "errors": errors}
        http_status = status.HTTP_207_MULTI_STATUS if errors else status.HTTP_200_OK
        return Response(payload, status=http_status)
