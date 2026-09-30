"""The DRF views answer a non-object body or a wrong-typed field with a 400, not a 500."""

from unittest.mock import MagicMock

import pytest
from django.test import Client

from apps.semantic.services import query as query_service
from apps.workspaces.models import (
    WorkspaceInvite,
    WorkspaceMembership,
    WorkspaceRole,
)

NON_STRINGS = {"number": 5, "list": ["x"], "object": {"a": 1}, "bool": True}
UNHASHABLE = {"list": ["x"], "object": {"a": 1}}


def _send(client, method, path, body):
    return getattr(client, method)(path, data=body, content_type="application/json")


@pytest.fixture
def client(user):
    c = Client(raise_request_exception=False)
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestNonObjectBody:
    @pytest.fixture
    def paths(self, workspace, other_user):
        member = WorkspaceMembership.objects.create(
            workspace=workspace, user=other_user, role=WorkspaceRole.READ
        )
        invite = WorkspaceInvite.objects.create(
            workspace=workspace, email="invitee@example.com", role=WorkspaceRole.READ
        )
        ws = f"/api/workspaces/{workspace.id}"
        return {
            "workspace-create": ("post", "/api/workspaces/"),
            "workspace-rename": ("patch", f"{ws}/"),
            "member-add": ("post", f"{ws}/members/"),
            "member-role": ("patch", f"{ws}/members/{member.id}/"),
            "invite-role": ("patch", f"{ws}/invites/{invite.id}/"),
            "tenant-add": ("post", f"{ws}/tenants/"),
            "knowledge-create": ("post", f"{ws}/knowledge/"),
            "semantic-query": ("post", f"{ws}/semantic-query/"),
        }

    @pytest.mark.parametrize(
        "endpoint",
        [
            "workspace-create",
            "workspace-rename",
            "member-add",
            "member-role",
            "invite-role",
            "tenant-add",
            "knowledge-create",
            "semantic-query",
        ],
    )
    @pytest.mark.parametrize(
        "body", [["x"], '"text"', "5", "null"], ids=["array", "string", "number", "null"]
    )
    def test_rejected(self, client, paths, endpoint, body):
        method, path = paths[endpoint]

        resp = _send(client, method, path, body)

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Request body must be a JSON object."


@pytest.mark.django_db
class TestFieldTypes:
    @pytest.mark.parametrize("value", NON_STRINGS.values(), ids=NON_STRINGS.keys())
    def test_member_email_must_be_a_string(self, client, workspace, value):
        body = {"email": value, "role": WorkspaceRole.READ}

        resp = _send(client, "post", f"/api/workspaces/{workspace.id}/members/", body)

        assert resp.status_code == 400
        assert resp.json()["error"] == "email must be a string."

    @pytest.mark.parametrize("value", UNHASHABLE.values(), ids=UNHASHABLE.keys())
    def test_knowledge_type_must_be_a_string(self, client, workspace, value):
        body = {"type": value, "title": "t", "content": "c"}

        resp = _send(client, "post", f"/api/workspaces/{workspace.id}/knowledge/", body)

        assert resp.status_code == 400
        assert resp.json()["error"] == "type must be a string."

    @pytest.mark.parametrize("value", UNHASHABLE.values(), ids=UNHASHABLE.keys())
    def test_workspace_tenant_id_must_be_a_string(self, client, workspace, value):
        resp = _send(
            client, "post", f"/api/workspaces/{workspace.id}/tenants/", {"tenant_id": value}
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == "tenant_id must be a string."

    def test_workspace_malformed_tenant_id_is_not_found(self, client, workspace):
        resp = _send(
            client, "post", f"/api/workspaces/{workspace.id}/tenants/", {"tenant_id": "nope"}
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == "Tenant not found or not accessible."


# transaction=True: the query service calls close_old_connections(), which closes a connection
# still inside pytest-django's per-test atomic block and breaks every later query.
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("granularity", [["day"], {"unit": "day"}], ids=["list", "object"])
def test_semantic_query_granularity_must_be_a_string(client, workspace, monkeypatch, granularity):
    monkeypatch.setattr(query_service, "get_active_semantic_model", lambda _ws: MagicMock())
    body = {
        "measures": ["visits.count"],
        "time_dimension": "visits.visit_date",
        "granularity": granularity,
    }

    resp = _send(client, "post", f"/api/workspaces/{workspace.id}/semantic-query/", body)

    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Unsupported granularity")
