"""Tests for workspace management API RBAC invariants (Task 3.1–3.3)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole

User = get_user_model()


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


@pytest.fixture
def manage_user(db, workspace):
    """The workspace fixture already gives `user` MANAGE role; return that user."""
    from tests.conftest import workspace as ws_fixture  # noqa: F401

    return workspace.memberships.get(role=WorkspaceRole.MANAGE).user


@pytest.fixture
def second_tenant(db):
    from apps.users.models import Tenant

    return Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other Domain"
    )


# ---------------------------------------------------------------------------
# Workspace list
# ---------------------------------------------------------------------------


class TestWorkspaceList:
    def test_list_returns_only_users_workspaces(self, client, user, workspace, db):
        other_user = User.objects.create_user(email="other@example.com", password="pass")
        other_ws = Workspace.objects.create(name="Other", created_by=other_user)
        WorkspaceMembership.objects.create(
            workspace=other_ws, user=other_user, role=WorkspaceRole.MANAGE
        )

        client.force_login(user)
        resp = client.get("/api/workspaces/")
        assert resp.status_code == 200
        ids = [w["id"] for w in resp.json()]
        assert str(workspace.id) in ids
        assert str(other_ws.id) not in ids

    def test_list_includes_role_and_counts(self, client, user, workspace):
        client.force_login(user)
        resp = client.get("/api/workspaces/")
        assert resp.status_code == 200
        entry = next(w for w in resp.json() if w["id"] == str(workspace.id))
        assert entry["role"] == WorkspaceRole.MANAGE
        assert entry["tenant_count"] == 1
        assert entry["member_count"] == 1

    def test_list_requires_authentication(self, client):
        resp = client.get("/api/workspaces/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Workspace create
# ---------------------------------------------------------------------------


class TestWorkspaceCreate:
    def test_create_workspace(self, client, user, tenant_membership):
        client.force_login(user)
        resp = client.post(
            "/api/workspaces/",
            {"name": "New workspace", "tenant_ids": [str(tenant_membership.tenant.id)]},
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "New workspace"
        assert WorkspaceMembership.objects.filter(
            workspace_id=resp.json()["id"], user=user, role=WorkspaceRole.MANAGE
        ).exists()

    def test_cannot_create_workspace_for_inaccessible_tenant(self, client, user, second_tenant, db):
        client.force_login(user)
        resp = client.post(
            "/api/workspaces/",
            {"name": "Bad", "tenant_ids": [str(second_tenant.id)]},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_create_requires_name(self, client, user, tenant_membership):
        client.force_login(user)
        resp = client.post(
            "/api/workspaces/",
            {"tenant_ids": [str(tenant_membership.tenant.id)]},
            content_type="application/json",
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Workspace rename (PATCH)
# ---------------------------------------------------------------------------


class TestWorkspaceRename:
    def test_manager_can_rename(self, client, user, workspace):
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"name": "Renamed"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        workspace.refresh_from_db()
        assert workspace.name == "Renamed"

    def test_non_manager_cannot_rename(self, client, workspace, db):
        write_user = User.objects.create_user(email="w@example.com", password="pass")
        WorkspaceMembership.objects.create(
            workspace=workspace, user=write_user, role=WorkspaceRole.READ_WRITE
        )
        client.force_login(write_user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"name": "Sneaky rename"},
            content_type="application/json",
        )
        assert resp.status_code == 403

    def test_non_member_gets_403(self, client, workspace, db):
        outsider = User.objects.create_user(email="out@example.com", password="pass")
        client.force_login(outsider)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/",
            {"name": "Whatever"},
            content_type="application/json",
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Workspace delete
# ---------------------------------------------------------------------------


class TestWorkspaceDelete:
    def test_cannot_delete_last_workspace_for_tenant(self, client, user, workspace):
        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/")
        assert resp.status_code == 400
        assert "last workspace" in resp.json()["error"].lower()

    def test_non_manager_cannot_delete(self, client, workspace, db):
        reader = User.objects.create_user(email="r@example.com", password="pass")
        WorkspaceMembership.objects.create(
            workspace=workspace, user=reader, role=WorkspaceRole.READ
        )
        client.force_login(reader)
        resp = client.delete(f"/api/workspaces/{workspace.id}/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Member management: last-manager guards
# ---------------------------------------------------------------------------


class TestMemberManagement:
    def test_cannot_demote_last_manager(self, client, user, workspace):
        membership = WorkspaceMembership.objects.get(workspace=workspace, user=user)
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/members/{membership.id}/",
            {"role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "last manager" in resp.json()["error"].lower()

    def test_cannot_remove_last_manager(self, client, user, workspace):
        membership = WorkspaceMembership.objects.get(workspace=workspace, user=user)
        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/members/{membership.id}/")
        assert resp.status_code == 400
        assert "last manager" in resp.json()["error"].lower()

    def test_second_manager_can_be_demoted(self, client, user, workspace, db):
        second = User.objects.create_user(email="mgr2@example.com", password="pass")
        second_membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=second, role=WorkspaceRole.MANAGE
        )
        client.force_login(user)
        resp = client.patch(
            f"/api/workspaces/{workspace.id}/members/{second_membership.id}/",
            {"role": WorkspaceRole.READ_WRITE},
            content_type="application/json",
        )
        assert resp.status_code == 200
        second_membership.refresh_from_db()
        assert second_membership.role == WorkspaceRole.READ_WRITE

    def test_removing_member_deletes_their_threads(self, client, user, workspace, db):
        from apps.chat.models import Thread

        writer = User.objects.create_user(email="wr@example.com", password="pass")
        writer_membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=writer, role=WorkspaceRole.READ_WRITE
        )
        thread = Thread.objects.create(workspace=workspace, user=writer, title="Writer thread")

        client.force_login(user)
        resp = client.delete(f"/api/workspaces/{workspace.id}/members/{writer_membership.id}/")
        assert resp.status_code == 204
        assert not Thread.objects.filter(id=thread.id).exists()

    def test_read_write_member_cannot_remove_others(self, client, workspace, db):
        writer = User.objects.create_user(email="wr@example.com", password="pass")
        WorkspaceMembership.objects.create(
            workspace=workspace, user=writer, role=WorkspaceRole.READ_WRITE
        )
        reader = User.objects.create_user(email="rd@example.com", password="pass")
        reader_membership = WorkspaceMembership.objects.create(
            workspace=workspace, user=reader, role=WorkspaceRole.READ
        )
        client.force_login(writer)
        resp = client.delete(f"/api/workspaces/{workspace.id}/members/{reader_membership.id}/")
        assert resp.status_code == 403
