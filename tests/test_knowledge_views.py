"""Tests for knowledge API views."""

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient


@pytest.fixture
def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_cannot_manually_create_agent_learning(api_client, workspace):
    url = reverse("knowledge:list_create", kwargs={"workspace_id": workspace.id})
    resp = api_client.post(url, {"type": "learning", "description": "test"})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "AgentLearning" in resp.data["error"]
