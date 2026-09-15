"""The workspace library lists artifacts, not every saved revision."""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.artifacts.models import Artifact, ArtifactType
from apps.users.models import TenantMembership
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def make_artifact(workspace, user):
    def make(parent=None, **kwargs):
        fields = {
            "workspace": workspace,
            "created_by": user,
            "title": "Topic overview",
            "description": "Current topics",
            "artifact_type": ArtifactType.MARKDOWN,
            "code": "# Topics",
            "conversation_id": "library-versions",
            "parent_artifact": parent,
            "version": parent.version + 1 if parent else 1,
        }
        return Artifact.objects.create(**{**fields, **kwargs})

    return make


@pytest.fixture
def library(client, user, workspace):
    client.force_login(user)

    def get(search=""):
        response = client.get(f"/api/workspaces/{workspace.id}/artifacts/", {"search": search})
        assert response.status_code == 200
        return response.json()["results"]

    return get


def test_library_collapses_versions_but_not_identical_independent_artifacts(library, make_artifact):
    first = make_artifact()
    second = make_artifact(first)
    latest = make_artifact(second)
    unrelated = make_artifact()

    results = library()

    assert [(item["id"], item["version"]) for item in results] == [
        (str(unrelated.id), 1),
        (str(latest.id), 3),
    ]


def test_library_newer_revision_wins_after_old_revision_metadata_edit(library, make_artifact):
    first = make_artifact()
    latest = make_artifact(first)
    Artifact.objects.filter(id=first.id).update(updated_at=timezone.now() + timedelta(days=1))

    assert [item["id"] for item in library()] == [str(latest.id)]


def test_library_forked_revisions_choose_one_latest_head(library, make_artifact):
    first = make_artifact()
    make_artifact(first)
    latest = make_artifact(first)

    assert [item["id"] for item in library()] == [str(latest.id)]


@pytest.mark.parametrize(
    ("deleted_indices", "expected_index"),
    [([0], 2), ([1], 2), ([2], 1), ([1, 2], 0), ([0, 1, 2], None)],
)
def test_library_uses_latest_nondeleted_revision_through_deleted_ancestors(
    library, make_artifact, user, deleted_indices, expected_index
):
    first = make_artifact()
    second = make_artifact(first)
    revisions = [first, second, make_artifact(second)]
    for index in deleted_indices:
        revisions[index].soft_delete(deleted_by=user)

    expected_ids = [] if expected_index is None else [str(revisions[expected_index].id)]
    assert [item["id"] for item in library()] == expected_ids
    assert Artifact.all_objects.count() == 3


def test_library_deleted_head_falls_back_and_undelete_restores_head(
    library, make_artifact, client, workspace
):
    first = make_artifact()
    latest = make_artifact(first)
    detail_url = f"/api/workspaces/{workspace.id}/artifacts/{latest.id}/"

    assert client.delete(detail_url).status_code == 204
    assert [item["id"] for item in library()] == [str(first.id)]
    assert client.post(f"{detail_url}undelete/").status_code == 200
    assert [item["id"] for item in library()] == [str(latest.id)]


@pytest.mark.parametrize(
    ("search", "matches"),
    [("old title", False), ("old description", False), ("NEW TITLE", True), ("new desc", True)],
)
def test_library_search_matches_current_head_only(library, make_artifact, search, matches):
    first = make_artifact(title="Old title", description="Old description")
    latest = make_artifact(first, title="New title", description="New description")

    expected_ids = [str(latest.id)] if matches else []
    assert [item["id"] for item in library(search)] == expected_ids


def test_library_search_uses_fallback_head_after_row_level_delete(library, make_artifact, user):
    first = make_artifact(title="Original topics")
    latest = make_artifact(first, title="Deleted revision")
    latest.soft_delete(deleted_by=user)

    assert [item["id"] for item in library("original")] == [str(first.id)]
    assert library("deleted") == []


def test_library_tracks_ancestry_through_unsupported_revisions(library, make_artifact):
    first = make_artifact()
    legacy = make_artifact(first, artifact_type="plotly")
    latest = make_artifact(legacy)
    make_artifact(latest, artifact_type="plotly")

    assert [item["id"] for item in library()] == [str(latest.id)]


def test_library_preserves_rows_old_urls_and_history(library, make_artifact, client, workspace):
    first = make_artifact(code="# Original")
    latest = make_artifact(first, code="# Updated")
    before = list(Artifact.all_objects.order_by("id").values())

    assert [item["id"] for item in library()] == [str(latest.id)]
    response = client.get(f"/api/workspaces/{workspace.id}/artifacts/{first.id}/data/")

    assert response.status_code == 200
    assert response.json()["code"] == "# Original"
    assert [item.id for item in latest.get_version_history()] == [first.id, latest.id]
    assert list(Artifact.all_objects.order_by("id").values()) == before


def test_library_never_follows_revisions_outside_authorized_workspace(
    library, make_artifact, client, workspace
):
    first = make_artifact()
    own_latest = make_artifact(first)
    foreign_workspace = Workspace.objects.create(name="Private workspace")
    foreign = make_artifact(own_latest, workspace=foreign_workspace, title="Private revision")
    local_with_foreign_parent = make_artifact(foreign)
    other_local_with_foreign_parent = make_artifact(foreign)

    assert {item["id"] for item in library()} == {
        str(own_latest.id),
        str(local_with_foreign_parent.id),
        str(other_local_with_foreign_parent.id),
    }
    assert client.get(f"/api/workspaces/{foreign_workspace.id}/artifacts/").status_code == 403
    response = client.get(f"/api/workspaces/{workspace.id}/artifacts/{foreign.id}/data/")
    assert response.status_code == 404


def test_library_requires_authentication(client, workspace, make_artifact):
    make_artifact()
    assert client.get(f"/api/workspaces/{workspace.id}/artifacts/").status_code == 401


def test_library_read_member_can_see_latest_revision(client, workspace, read_user, make_artifact):
    first = make_artifact()
    latest = make_artifact(first)
    client.force_login(read_user)

    response = client.get(f"/api/workspaces/{workspace.id}/artifacts/")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["results"]] == [str(latest.id)]


def test_library_denies_revoked_upstream_access(client, workspace, user, tenant, make_artifact):
    make_artifact()
    client.force_login(user)
    TenantMembership.objects.filter(user=user, tenant=tenant).update(archived_at=timezone.now())

    assert client.get(f"/api/workspaces/{workspace.id}/artifacts/").status_code == 403


def test_library_ancestry_lookup_does_not_fetch_historical_payloads_or_n_plus_one(
    library, make_artifact
):
    latest = None
    for _ in range(20):
        latest = make_artifact(latest, code="historical payload")

    with CaptureQueriesContext(connection) as queries:
        results = library()

    assert [item["id"] for item in results] == [str(latest.id)]
    artifact_queries = [
        query["sql"] for query in queries if 'FROM "artifacts_artifact"' in query["sql"]
    ]
    assert len(artifact_queries) == 2
    assert '"artifacts_artifact"."code"' not in artifact_queries[0]
    assert '"artifacts_artifact"."data"' not in artifact_queries[0]


def test_library_malformed_lineage_cycle_is_bounded(library, make_artifact):
    first = make_artifact()
    latest = make_artifact(first)
    Artifact.objects.filter(id=first.id).update(parent_artifact=latest)

    assert [item["id"] for item in library()] == [str(latest.id)]
