"""Tests for workspace permission classes and URL structure (Tasks 2.3, 2.4)."""

import pytest
from django.test import Client


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


@pytest.fixture
def auth_client(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def read_client(client, read_user):
    client.force_login(read_user)
    return client


@pytest.fixture
def write_client(client, write_user):
    client.force_login(write_user)
    return client


# --- Workspace list ---


@pytest.mark.django_db
def test_workspace_list_returns_user_workspaces(auth_client, workspace):
    resp = auth_client.get("/api/workspaces/")
    assert resp.status_code == 200
    ids = [w["id"] for w in resp.json()]
    assert str(workspace.id) in ids


@pytest.mark.django_db
def test_workspace_list_excludes_other_workspaces(client, workspace, other_user):
    client.force_login(other_user)
    resp = client.get("/api/workspaces/")
    assert resp.status_code == 200
    ids = [w["id"] for w in resp.json()]
    assert str(workspace.id) not in ids


# --- Workspace detail ---


@pytest.mark.django_db
def test_workspace_detail_accessible_to_member(auth_client, workspace):
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(workspace.id)


@pytest.mark.django_db
def test_workspace_detail_denied_to_non_member(client, workspace, other_user):
    client.force_login(other_user)
    resp = client.get(f"/api/workspaces/{workspace.id}/")
    assert resp.status_code == 403


# --- Content URLs under workspace ---


@pytest.mark.django_db
def test_artifacts_nested_under_workspace(auth_client, workspace):
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/artifacts/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_recipes_nested_under_workspace(auth_client, workspace):
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/recipes/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_knowledge_nested_under_workspace(auth_client, workspace):
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/knowledge/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_threads_nested_under_workspace(auth_client, workspace):
    resp = auth_client.get(f"/api/workspaces/{workspace.id}/threads/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_non_member_cannot_access_workspace_artifacts(client, workspace, other_user):
    client.force_login(other_user)
    resp = client.get(f"/api/workspaces/{workspace.id}/artifacts/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_non_member_cannot_access_workspace_knowledge(client, workspace, other_user):
    client.force_login(other_user)
    resp = client.get(f"/api/workspaces/{workspace.id}/knowledge/")
    assert resp.status_code == 403
