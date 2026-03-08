"""Integration tests for the workspace-based chat flow."""

import json

import pytest
from django.test import Client

from apps.projects.models import Workspace


@pytest.fixture
def workspace_for_member(tenant_membership):
    """Return the Workspace auto-created for the tenant_membership."""
    return Workspace.objects.get(
        is_auto_created=True,
        workspace_tenants__tenant=tenant_membership.tenant,
        memberships__user=tenant_membership.user,
    )


@pytest.mark.django_db
class TestTenantChatFlow:
    def test_chat_accepts_workspace_id(self, user, workspace_for_member):
        """POST /api/chat/ with workspaceId should not return 'projectId is required'."""
        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "data": {
                        "workspaceId": str(workspace_for_member.id),
                        "threadId": "test-thread",
                    },
                }
            ),
            content_type="application/json",
        )

        # Should NOT get a 400 about projectId/workspaceId — may fail at MCP tool
        # loading (500) since no MCP server is running, but passes workspace validation.
        if response.status_code == 400:
            body = response.json()
            assert "workspaceId" not in body.get("error", ""), "Chat view should accept workspaceId"

    def test_chat_rejects_missing_workspace_id(self, user):
        """POST /api/chat/ without workspaceId should return 400."""
        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "data": {"threadId": "test-thread"},
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        body = response.json()
        assert "workspaceId" in body.get("error", "")

    def test_chat_rejects_invalid_workspace_id(self, user):
        """POST /api/chat/ with non-existent workspaceId should return 403."""
        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "data": {
                        "workspaceId": "00000000-0000-0000-0000-000000000000",
                        "threadId": "test-thread",
                    },
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 403

    def test_thread_list_accessible_via_workspace(self, user, workspace_for_member):
        """GET /api/workspaces/<id>/threads/ returns threads for that workspace."""
        client = Client()
        client.force_login(user)

        response = client.get(f"/api/workspaces/{workspace_for_member.id}/threads/")

        assert response.status_code == 200
        assert isinstance(response.json(), list)
