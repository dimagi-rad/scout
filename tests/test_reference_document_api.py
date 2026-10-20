"""Knowledge API for workspace reference documents (#913)."""

import io
import json
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.knowledge.models import KnowledgeEntry
from apps.knowledge.services.reference_documents import MAX_UPLOAD_BYTES
from tests.test_reference_documents import REGISTRY, REGISTRY_TEXT


def _reference(workspace, title="Indicator registry", content=REGISTRY_TEXT, **fields):
    return KnowledgeEntry.objects.create(
        workspace=workspace,
        title=title,
        content=content,
        kind=KnowledgeEntry.Kind.REFERENCE,
        source_format=fields.pop("source_format", KnowledgeEntry.SourceFormat.JSON),
        **fields,
    )


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _upload(client, workspace, name, data: bytes, **extra):
    url = reverse("knowledge:upload", kwargs={"workspace_id": workspace.id})
    return client.post(url, {"file": SimpleUploadedFile(name, data), **extra}, format="multipart")


@pytest.mark.django_db
class TestUploadView:
    def test_upload_creates_a_reference_document(self, api_client, workspace, user):
        resp = _upload(api_client, workspace, "indicator-registry.json", REGISTRY_TEXT.encode())

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        assert resp.data["kind"] == "reference"
        assert resp.data["title"] == "indicator-registry"
        assert resp.data["size"] == len(REGISTRY_TEXT)
        # Responses carry a preview; only the detail GET returns the whole document.
        assert resp.data["content"] == REGISTRY_TEXT[:500]
        assert resp.data["content_truncated"] is True
        entry = KnowledgeEntry.objects.get(pk=resp.data["id"])
        assert entry.content == REGISTRY_TEXT
        assert entry.source_format == "json"
        assert entry.created_by == user
        assert entry.description.startswith("JSON array of 400 entries")

    def test_title_and_description_can_be_given(self, api_client, workspace):
        resp = _upload(
            api_client,
            workspace,
            "codes.csv",
            b"code,name\nA,Alpha\n",
            title="  Facility codes ",
            description="Facility code list\nfrom the MoH",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        assert resp.data["title"] == "Facility codes"
        assert resp.data["description"] == "Facility code list from the MoH"

    def test_rejects_unsupported_type(self, api_client, workspace):
        resp = _upload(api_client, workspace, "registry.pdf", b"%PDF")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "Unsupported file type" in resp.data["error"]
        assert not KnowledgeEntry.objects.exists()

    def test_rejects_oversized_file(self, api_client, workspace):
        resp = _upload(api_client, workspace, "big.txt", b"a" * (MAX_UPLOAD_BYTES + 1))
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "too large" in resp.data["error"]

    def test_read_only_member_cannot_upload(self, workspace, read_user):
        client = APIClient()
        client.force_authenticate(user=read_user)
        resp = _upload(client, workspace, "codes.csv", b"code\nA\n")
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert not KnowledgeEntry.objects.exists()

    def test_list_search_skips_reference_bodies(self, api_client, workspace):
        _reference(workspace, description="Official registry")
        KnowledgeEntry.objects.create(workspace=workspace, title="Note", content="IND007 rule")
        url = reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id})

        def titles(search):
            return [
                item["title"] for item in api_client.get(url, {"search": search}).data["results"]
            ]

        assert titles("IND007") == ["Note"]
        assert titles("official") == ["Indicator registry"]

    def test_list_previews_reference_docs_and_detail_has_all(self, api_client, workspace):
        doc = _reference(workspace)
        KnowledgeEntry.objects.create(workspace=workspace, title="Note", content="short note")

        resp = api_client.get(
            reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id})
        )

        by_title = {item["title"]: item for item in resp.data["results"]}
        listed = by_title["Indicator registry"]
        assert listed["content_truncated"] is True
        assert listed["content"] == REGISTRY_TEXT[:500]
        assert listed["size"] == len(REGISTRY_TEXT)
        assert by_title["Note"]["content"] == "short note"
        assert by_title["Note"]["content_truncated"] is False
        assert by_title["Note"]["kind"] == "note"

        detail = api_client.get(
            reverse("knowledge:detail", kwargs={"workspace_id": workspace.id, "item_id": doc.id})
        )
        assert detail.data["content"] == REGISTRY_TEXT
        assert detail.data["content_truncated"] is False

    def test_echoed_preview_does_not_truncate_the_document(self, api_client, workspace):
        doc = _reference(workspace)
        url = reverse("knowledge:detail", kwargs={"workspace_id": workspace.id, "item_id": doc.id})
        listed = api_client.get(
            reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id})
        ).data["results"][0]

        resp = api_client.put(
            url, {"title": "Renamed", "content": listed["content"], "tags": []}, format="json"
        )

        assert resp.status_code == status.HTTP_200_OK, resp.data
        doc.refresh_from_db()
        assert (doc.title, doc.content) == ("Renamed", REGISTRY_TEXT)

    def test_edited_preview_is_refused_not_written(self, api_client, workspace):
        doc = _reference(workspace)
        url = reverse("knowledge:detail", kwargs={"workspace_id": workspace.id, "item_id": doc.id})

        resp = api_client.put(
            url, {"title": "Renamed", "content": REGISTRY_TEXT[:500] + " edited"}, format="json"
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "Upload a new version" in str(resp.data["content"])
        doc.refresh_from_db()
        assert (doc.title, doc.content) == ("Indicator registry", REGISTRY_TEXT)

    def test_upload_keeps_the_file_byte_for_byte(self, api_client, workspace):
        resp = _upload(api_client, workspace, "notes.md", b"\n# Codes\n\nA: Alpha\n")
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        assert KnowledgeEntry.objects.get().content == "\n# Codes\n\nA: Alpha\n"

    def test_blank_description_keeps_the_json_summary(self, api_client, workspace):
        resp = _upload(
            api_client, workspace, "registry.json", REGISTRY_TEXT.encode(), description="   "
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        assert resp.data["description"].startswith("JSON array of 400 entries")

    def test_import_rejects_binary_and_oversized_reference_documents(self, api_client, workspace):
        meta = "---\ntitle: {title}\nkind: reference\nsource_format: txt\n---\n\n"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("nul.md", meta.format(title="Nul") + "a\x00b")
            zf.writestr("big.md", meta.format(title="Big") + "a" * (MAX_UPLOAD_BYTES + 1))
            zf.writestr("ok.md", meta.format(title="Ok") + "fine")
        archive.seek(0)
        archive.name = "import.zip"

        resp = api_client.post(
            reverse("knowledge:import", kwargs={"workspace_id": workspace.id}),
            {"file": archive},
            format="multipart",
        )

        assert resp.status_code == status.HTTP_207_MULTI_STATUS, resp.data
        assert sorted(error["file"] for error in resp.data["errors"]) == ["big.md", "nul.md"]
        assert list(KnowledgeEntry.objects.values_list("title", "kind")) == [("Ok", "reference")]

    def test_import_of_a_note_does_not_replace_a_reference_document(self, api_client, workspace):
        _reference(workspace)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("old.md", "---\ntitle: Indicator registry\n---\n\nold note body")
        archive.seek(0)
        archive.name = "import.zip"

        resp = api_client.post(
            reverse("knowledge:import", kwargs={"workspace_id": workspace.id}),
            {"file": archive},
            format="multipart",
        )

        assert resp.status_code == status.HTTP_207_MULTI_STATUS, resp.data
        entry = KnowledgeEntry.objects.get()
        assert (entry.kind, entry.content) == ("reference", REGISTRY_TEXT)

    def test_upload_rejects_nul_in_title(self, api_client, workspace):
        resp = _upload(api_client, workspace, "codes.csv", b"code\nA\n", title="bad\x00title")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert not KnowledgeEntry.objects.exists()

    def test_kind_cannot_be_set_through_the_entry_api(self, api_client, workspace):
        created = api_client.post(
            reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id}),
            {"type": "entry", "title": "Sneaky", "content": "body", "kind": "reference"},
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data
        assert created.data["kind"] == "note"

        doc = _reference(workspace)
        api_client.put(
            reverse("knowledge:detail", kwargs={"workspace_id": workspace.id, "item_id": doc.id}),
            {"kind": "note", "source_format": "txt"},
            format="json",
        )
        doc.refresh_from_db()
        assert (doc.kind, doc.source_format) == ("reference", "json")

    def test_note_content_is_still_trimmed(self, api_client, workspace):
        resp = api_client.post(
            reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id}),
            {"type": "entry", "title": "Note", "content": "  body \n"},
            format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        assert KnowledgeEntry.objects.get().content == "body"

    def test_metadata_edit_keeps_content(self, api_client, workspace):
        doc = _reference(workspace)
        resp = api_client.put(
            reverse("knowledge:detail", kwargs={"workspace_id": workspace.id, "item_id": doc.id}),
            {"title": "Registry v2", "description": "Updated"},
            format="json",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.data
        assert resp.data["content_truncated"] is True
        doc.refresh_from_db()
        assert (doc.title, doc.description, doc.content) == (
            "Registry v2",
            "Updated",
            REGISTRY_TEXT,
        )

    def test_export_import_round_trips_reference_docs(self, api_client, workspace):
        _reference(workspace, description="Official registry")
        export = api_client.get(reverse("knowledge:export", kwargs={"workspace_id": workspace.id}))
        KnowledgeEntry.objects.all().delete()

        archive = io.BytesIO(export.content)
        archive.name = "import.zip"
        resp = api_client.post(
            reverse("knowledge:import", kwargs={"workspace_id": workspace.id}),
            {"file": archive},
            format="multipart",
        )

        assert resp.status_code == status.HTTP_200_OK, resp.data
        entry = KnowledgeEntry.objects.get()
        assert entry.kind == "reference"
        assert entry.source_format == "json"
        assert entry.description == "Official registry"
        assert json.loads(entry.content) == REGISTRY
        with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
            assert "kind: reference" in zf.read(zf.namelist()[0]).decode()
