"""API views for knowledge management."""

import io
import logging
import zipfile

import yaml
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Case, F, Q, TextField, When
from django.db.models.functions import Length, Substr
from django.http import HttpResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.http import string_field
from apps.knowledge.models import KnowledgeEntry
from apps.knowledge.services.reference_documents import (
    DESCRIPTION_MAX_CHARS,
    MAX_UPLOAD_BYTES,
    ReferenceDocumentError,
    decode_upload,
)
from apps.knowledge.utils import parse_frontmatter, render_frontmatter
from apps.workspaces.models import WorkspaceRole
from apps.workspaces.workspace_resolver import resolve_workspace_drf as resolve_workspace

from .serializers import REFERENCE_PREVIEW_CHARS, KnowledgeEntrySerializer

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Cap decompressed import size to prevent a zip bomb inflating the knowledge
# base, which is re-billed into the system prompt on every LLM call (arch #254, 01#4).
MAX_IMPORT_DECOMPRESSED_BYTES = 25 * 1024 * 1024


def _list_entries(queryset):
    """Defer ``content`` so a page of multi-megabyte reference documents isn't loaded."""
    return queryset.defer("content").annotate(
        content_size=Length("content"),
        listed_content=Case(
            When(
                kind=KnowledgeEntry.Kind.REFERENCE,
                then=Substr("content", 1, REFERENCE_PREVIEW_CHARS),
            ),
            default=F("content"),
            output_field=TextField(),
        ),
    )


KNOWLEDGE_TYPES = {
    "entry": {
        "model": KnowledgeEntry,
        "serializer": KnowledgeEntrySerializer,
        "search_fields": ["title", "content"],
        # Join created_by so the page slice doesn't N+1 (arch #254, 05#7).
        "select_related": ["created_by"],
        "list_queryset": _list_entries,
    },
}
# AgentLearning rows are workspace memory now (#849): listed and changed only through
# /api/workspaces/<id>/memory/, which enforces author-or-manager edits and audits them.


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

        # Paginate in the DB, not in Python (arch #254, finding 05#7): fetch only up
        # to end_index per type, merge by created_at desc, then slice — so
        # serialization is bounded by page*page_size per type, not the full table.
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
            list_queryset = type_config.get("list_queryset")
            if list_queryset:
                page_window = list_queryset(page_window)
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
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
        if err:
            return err

        item_type, err = string_field(request.data, "type")
        if err:
            return err
        # Kept so a stale client gets a pointer rather than "invalid type".
        if item_type == "learning":
            return Response(
                {"error": "Workspace memories are managed on the Memory page."},
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

        instance.created_by = request.user
        instance.save(update_fields=["created_by"])

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

        serializer = serializer_class(item, context={"full_content": True})
        return Response(serializer.data)

    def put(self, request, workspace_id, item_id):
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
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
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
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
                content = render_frontmatter(
                    entry.title, entry.tags or [], entry.content, extra=_reference_meta(entry)
                )
                zf.writestr(filename, content)

        buf.seek(0)
        tenants = list(workspace.tenants.all()[:2])
        name = tenants[0].external_id if len(tenants) == 1 else str(workspace.id)
        safe_name = name.replace("/", "_")
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
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
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
                        title, tags, body, meta = parse_frontmatter(raw)
                    except (ValueError, yaml.YAMLError) as exc:
                        errors.append({"file": name, "error": f"Malformed frontmatter: {exc}"})
                        continue
                    fields = {"content": body, "tags": tags, **_reference_fields(meta)}
                    # Postgres can't store NUL, and would fail the whole batch on it.
                    if "\x00" in body:
                        errors.append({"file": name, "error": "File contains binary data."})
                        continue
                    if (
                        fields["kind"] == KnowledgeEntry.Kind.REFERENCE
                        and len(body.encode()) > MAX_UPLOAD_BYTES
                    ):
                        errors.append(
                            {"file": name, "error": "Reference document is larger than 2 MB."}
                        )
                        continue

                    if not title:
                        skipped += 1
                        continue

                    pool = available_by_title.get(title)
                    if pool:
                        pk = pool.pop(0)
                        KnowledgeEntry.objects.filter(pk=pk).update(**fields)
                        updated += 1
                    else:
                        KnowledgeEntry.objects.create(
                            workspace=workspace, title=title, created_by=request.user, **fields
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


def _reference_meta(entry: KnowledgeEntry) -> dict:
    meta = {}
    if entry.kind == KnowledgeEntry.Kind.REFERENCE:
        meta["kind"] = entry.kind
    if entry.description:
        meta["description"] = entry.description
    if entry.source_format:
        meta["source_format"] = entry.source_format
    return meta


def _reference_fields(meta: dict) -> dict:
    """An imported entry's kind fields, so a reference document round-trips as one."""
    is_reference = meta.get("kind") == KnowledgeEntry.Kind.REFERENCE
    source_format = meta.get("source_format", "")
    return {
        "kind": KnowledgeEntry.Kind.REFERENCE if is_reference else KnowledgeEntry.Kind.NOTE,
        "description": " ".join(str(meta.get("description") or "").split())[:DESCRIPTION_MAX_CHARS],
        "source_format": (
            source_format
            if is_reference and source_format in KnowledgeEntry.SourceFormat.values
            else ""
        ),
    }


class KnowledgeUploadView(APIView):
    """
    POST /api/workspaces/<id>/knowledge/upload/

    Upload one file (JSON, CSV, TSV, Markdown or text; UTF-8; up to 2 MB) as a
    reference document (#913). ``title`` and ``description`` are optional; the
    title defaults to the file name.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request, workspace_id):
        workspace, _membership, err = resolve_workspace(
            request, workspace_id, minimum_role=WorkspaceRole.READ_WRITE
        )
        if err:
            return err

        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response(
                {"error": "No file uploaded. Send the document as 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Checked before reading so an oversized upload is never pulled into memory.
        if uploaded.size > MAX_UPLOAD_BYTES:
            return Response(
                {
                    "error": (
                        f"File is too large ({uploaded.size:,} bytes); the limit is "
                        f"{MAX_UPLOAD_BYTES:,} bytes."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            decoded = decode_upload(uploaded.name, uploaded.read())
        except ReferenceDocumentError as exc:
            return Response({"error": exc.user_message}, status=status.HTTP_400_BAD_REQUEST)

        title = str(request.data.get("title") or "").strip() or decoded.title
        description = (
            " ".join(str(request.data.get("description") or "").split()) or decoded.description
        )
        # Built here rather than through the serializer, where kind and format are
        # read-only so only this path (and import) can create a reference document.
        entry = KnowledgeEntry(
            workspace=workspace,
            created_by=request.user,
            title=title,
            description=" ".join(description.split())[:DESCRIPTION_MAX_CHARS],
            content=decoded.content,
            kind=KnowledgeEntry.Kind.REFERENCE,
            source_format=decoded.source_format,
            tags=[],
        )
        try:
            entry.full_clean()
        except DjangoValidationError as exc:
            return Response(exc.message_dict, status=status.HTTP_400_BAD_REQUEST)
        entry.save()
        return Response(KnowledgeEntrySerializer(entry).data, status=status.HTTP_201_CREATED)
