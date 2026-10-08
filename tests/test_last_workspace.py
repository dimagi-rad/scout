"""The server remembers the workspace a user was last in."""

import pytest

from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole

URL = "/api/auth/last-workspace/"


def _remember(client, workspace_id):
    return client.post(URL, {"workspace_id": str(workspace_id)}, content_type="application/json")


@pytest.mark.django_db
class TestLastWorkspace:
    def test_me_has_no_last_workspace_by_default(self, client, user):
        client.force_login(user)
        assert client.get("/api/auth/me/").json()["last_workspace_id"] is None

    def test_remember_then_me_returns_it(self, client, user, workspace):
        client.force_login(user)
        assert _remember(client, workspace.id).status_code == 200
        user.refresh_from_db()
        assert user.last_workspace_id == workspace.id
        assert client.get("/api/auth/me/").json()["last_workspace_id"] == str(workspace.id)

    def test_login_returns_it(self, client, user, workspace):
        user.last_workspace = workspace
        user.save()
        resp = client.post(
            "/api/auth/login/",
            {"email": user.email, "password": "testpass123"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json()["last_workspace_id"] == str(workspace.id)

    def test_cannot_remember_a_workspace_without_membership(self, client, user, other_user):
        foreign = Workspace.objects.create(name="Foreign", created_by=other_user)
        WorkspaceMembership.objects.create(
            workspace=foreign, user=other_user, role=WorkspaceRole.MANAGE
        )
        client.force_login(user)
        assert _remember(client, foreign.id).status_code == 404
        user.refresh_from_db()
        assert user.last_workspace_id is None

    def test_malformed_id_is_not_found(self, client, user):
        client.force_login(user)
        assert _remember(client, "not-a-uuid").status_code == 404

    def test_lost_membership_is_not_returned(self, client, user, workspace):
        client.force_login(user)
        _remember(client, workspace.id)
        WorkspaceMembership.objects.filter(user=user, workspace=workspace).delete()
        assert client.get("/api/auth/me/").json()["last_workspace_id"] is None

    def test_deleted_workspace_clears_pointer(self, client, user, workspace):
        client.force_login(user)
        _remember(client, workspace.id)
        workspace.delete()
        user.refresh_from_db()
        assert user.last_workspace_id is None
        assert client.get("/api/auth/me/").json()["last_workspace_id"] is None

    def test_requires_login(self, client, workspace):
        assert _remember(client, workspace.id).status_code == 401
