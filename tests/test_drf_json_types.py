"""The DRF views answer a non-object body with a 400, not a 500."""

import pytest
from django.test import Client

from apps.workspaces.models import (
    WorkspaceInvite,
    WorkspaceMembership,
    WorkspaceRole,
)


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
            "transformations-trigger": ("post", "/api/transformations/runs/trigger/"),
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
            "transformations-trigger",
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
