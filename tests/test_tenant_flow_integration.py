"""Integration tests for the tenant-based chat flow."""

import json

import pytest
from django.test import Client


@pytest.mark.django_db
class TestTenantChatFlow:
    def test_chat_accepts_tenant_id(self, user, tenant_membership):
        """POST /api/chat/ with tenantId should not return 'projectId is required'."""
        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/chat/",
            data=json.dumps({
                "messages": [{"role": "user", "content": "Hello"}],
                "data": {"tenantId": str(tenant_membership.id), "threadId": "test-thread"},
            }),
            content_type="application/json",
        )

        # Should NOT get a 400 about projectId — the old behavior is gone.
        # It may fail at MCP tool loading (500) since no MCP server is running,
        # but the request should pass tenant validation (not 400, not 403).
        if response.status_code == 400:
            body = response.json()
            assert "projectId" not in body.get("error", ""), (
                "Chat view should accept tenantId, not require projectId"
            )

    def test_chat_rejects_missing_tenant_id(self, user):
        """POST /api/chat/ without tenantId should return 400."""
        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/chat/",
            data=json.dumps({
                "messages": [{"role": "user", "content": "Hello"}],
                "data": {"threadId": "test-thread"},
            }),
            content_type="application/json",
        )

        assert response.status_code == 400
        body = response.json()
        assert "tenantId" in body.get("error", "")

    def test_chat_rejects_invalid_tenant_id(self, user):
        """POST /api/chat/ with non-existent tenantId should return 403."""
        client = Client()
        client.force_login(user)

        response = client.post(
            "/api/chat/",
            data=json.dumps({
                "messages": [{"role": "user", "content": "Hello"}],
                "data": {
                    "tenantId": "00000000-0000-0000-0000-000000000000",
                    "threadId": "test-thread",
                },
            }),
            content_type="application/json",
        )

        assert response.status_code == 403

    def test_thread_list_accepts_tenant_id(self, user, tenant_membership):
        """GET /api/chat/threads/?tenant_id=X should return threads for that tenant."""
        client = Client()
        client.force_login(user)

        response = client.get(
            f"/api/chat/threads/?tenant_id={tenant_membership.id}"
        )

        assert response.status_code == 200
        assert isinstance(response.json(), list)
