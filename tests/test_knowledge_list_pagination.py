"""Knowledge list DB-level pagination (arch #254, finding 05#7).

GET /api/knowledge/ previously loaded and serialized EVERY KnowledgeEntry and
AgentLearning in the workspace, merged and sorted the serialized dicts in
Python, then sliced the requested page — O(total) work per page view. This
paginates in the DB query so the work is bounded by page_size.
"""

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.knowledge.models import AgentLearning, KnowledgeEntry


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _seed(workspace, user, n_entries=30, n_learnings=30):
    for i in range(n_entries):
        KnowledgeEntry.objects.create(
            workspace=workspace, title=f"Entry {i:03d}", content="c", created_by=user
        )
    for i in range(n_learnings):
        AgentLearning.objects.create(
            workspace=workspace,
            description=f"Learning {i:03d}",
            category="other",
            is_active=True,
            discovered_by_user=user,
        )


@pytest.mark.django_db
def test_list_returns_only_requested_page(api_client, workspace, user):
    _seed(workspace, user)
    url = reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id})
    resp = api_client.get(url, {"page": 1, "page_size": 10})
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert len(body["results"]) == 10
    assert body["pagination"]["total_count"] == 60
    assert body["pagination"]["total_pages"] == 6
    assert body["pagination"]["has_next"] is True


@pytest.mark.django_db
def test_second_page_reachable(api_client, workspace, user):
    _seed(workspace, user, n_entries=5, n_learnings=0)
    url = reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id})
    p1 = api_client.get(url, {"page": 1, "page_size": 2}).json()
    p2 = api_client.get(url, {"page": 2, "page_size": 2}).json()
    ids1 = {item["id"] for item in p1["results"]}
    ids2 = {item["id"] for item in p2["results"]}
    assert len(ids1) == 2
    assert len(ids2) == 2
    assert ids1.isdisjoint(ids2), "page 2 must contain different items than page 1"


@pytest.mark.django_db
def test_pagination_does_not_serialize_all_rows(
    api_client, workspace, user, django_assert_max_num_queries
):
    """Per-page DB work must NOT scale with the full row count (05#7).

    With DB-level pagination the query count is a small constant regardless of
    how many rows exist. (The old impl serialized every row every request.)
    """
    _seed(workspace, user, n_entries=100, n_learnings=100)
    url = reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id})
    # A small fixed budget: per-type count + per-type page slice (+ auth/session).
    with django_assert_max_num_queries(12):
        resp = api_client.get(url, {"page": 1, "page_size": 10})
    assert resp.status_code == status.HTTP_200_OK
    assert len(resp.json()["results"]) == 10
