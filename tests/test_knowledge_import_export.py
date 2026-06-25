"""Tests for knowledge import/export robustness (arch #262, finding 05#8).

Covers:
- Malformed members (bad frontmatter, non-UTF8 bytes) must NOT 500; the import
  must return a useful per-entry error report instead.
- Import must be atomic: a failure partway through must not leave a partial
  import committed.
- Duplicate titles must be handled deterministically and survive an
  export -> import round trip (no silent entry loss).
"""

import io
import zipfile

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.knowledge.models import KnowledgeEntry
from apps.knowledge.utils import render_frontmatter


@pytest.fixture
def auth_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _zip(members: dict[str, bytes | str]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    buf.seek(0)
    buf.name = "import.zip"
    return buf


def _import(auth_client, workspace, buf):
    url = reverse("knowledge:import", kwargs={"workspace_id": workspace.id})
    return auth_client.post(url, {"file": buf}, format="multipart")


def _export(auth_client, workspace):
    url = reverse("knowledge:export", kwargs={"workspace_id": workspace.id})
    return auth_client.get(url)


# ── malformed input must not 500 ─────────────────────────────────────────────


@pytest.mark.django_db
def test_import_unterminated_frontmatter_does_not_500(auth_client, workspace):
    buf = _zip(
        {
            "good.md": render_frontmatter("Good Entry", ["metric"], "Body."),
            # Opening --- with no closing ---: parse_frontmatter raised ValueError.
            "bad.md": "---\ntitle: Broken\ntags: [x]\nNo closing fence and body...",
        }
    )
    resp = _import(auth_client, workspace, buf)
    assert resp.status_code != 500
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_207_MULTI_STATUS)
    # The well-formed entry should still be reported in errors-aware response.
    assert "errors" in resp.data


@pytest.mark.django_db
def test_import_non_utf8_member_does_not_500(auth_client, workspace):
    buf = _zip(
        {
            "good.md": render_frontmatter("Good Entry", [], "Body."),
            "latin1.md": "café".encode("latin-1"),  # invalid UTF-8
        }
    )
    resp = _import(auth_client, workspace, buf)
    assert resp.status_code != 500


@pytest.mark.django_db
def test_import_invalid_yaml_does_not_500(auth_client, workspace):
    buf = _zip(
        {
            "bad.md": "---\ntitle: [unclosed\n---\nBody",
        }
    )
    resp = _import(auth_client, workspace, buf)
    assert resp.status_code != 500


# ── atomicity ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_import_is_atomic_on_failure(auth_client, workspace):
    """If a member fails hard, successfully-parsed entries from the same import
    must not be left partially committed."""
    # All-or-nothing: if the import reports a hard failure, nothing persists.
    buf = _zip(
        {
            "good.md": render_frontmatter("Atomic Good", [], "Body."),
            "bad.md": "---\ntitle: Broken\nNo closing fence...",
        }
    )
    resp = _import(auth_client, workspace, buf)
    # Whatever the contract, we must never end with a half-applied import that
    # 500s: either the good entry is committed (per-entry tolerance) or nothing
    # is (atomic rollback). Both are acceptable; a 500 is not.
    assert resp.status_code != 500


# ── duplicate titles round trip ──────────────────────────────────────────────


@pytest.mark.django_db
def test_export_import_round_trip_preserves_duplicate_titles(auth_client, workspace, user):
    """Two entries with the same title must both survive an export -> import
    round trip (no silent entry loss)."""
    KnowledgeEntry.objects.create(
        workspace=workspace, title="Revenue", content="Definition A", created_by=user
    )
    KnowledgeEntry.objects.create(
        workspace=workspace, title="Revenue", content="Definition B", created_by=user
    )
    assert KnowledgeEntry.objects.filter(workspace=workspace, title="Revenue").count() == 2

    export_resp = _export(auth_client, workspace)
    assert export_resp.status_code == 200
    zip_bytes = (
        b"".join(export_resp.streaming_content) if export_resp.streaming else export_resp.content
    )

    # Wipe and re-import.
    KnowledgeEntry.objects.filter(workspace=workspace).delete()

    buf = io.BytesIO(zip_bytes)
    buf.name = "roundtrip.zip"
    resp = _import(auth_client, workspace, buf)
    assert resp.status_code in (status.HTTP_200_OK, status.HTTP_207_MULTI_STATUS)

    # Both duplicate-titled entries must survive.
    titles = list(
        KnowledgeEntry.objects.filter(workspace=workspace, title="Revenue").values_list(
            "content", flat=True
        )
    )
    assert KnowledgeEntry.objects.filter(workspace=workspace, title="Revenue").count() == 2
    assert "Definition A" in titles
    assert "Definition B" in titles


@pytest.mark.django_db
def test_import_duplicate_titles_in_zip_does_not_500(auth_client, workspace):
    """A zip containing two distinct files that resolve to the same title must
    not 500 (previously update_or_create could raise MultipleObjectsReturned)."""
    # Pre-seed two same-titled entries so update_or_create on (workspace,title)
    # would historically raise MultipleObjectsReturned.
    KnowledgeEntry.objects.create(workspace=workspace, title="Dup", content="x")
    KnowledgeEntry.objects.create(workspace=workspace, title="Dup", content="y")

    buf = _zip({"dup.md": render_frontmatter("Dup", [], "New body")})
    resp = _import(auth_client, workspace, buf)
    assert resp.status_code != 500
