"""A workspace with no sources can't be created or emptied (#381); one that exists
anyway is denied rather than opened to every member on membership alone."""

import logging

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import Client

from apps.workspaces.access import (
    NO_SOURCES,
    aresolve_local_access_many,
    aresolve_workspace_access_ex,
    resolve_workspace_access_ex,
)
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole

User = get_user_model()

LOGGER = "apps.workspaces.access"
EVENT = "workspace_access_denied_no_sources"


def _empty_workspace(email):
    user = User.objects.create_user(email=email, password="pass")
    ws = Workspace.objects.create(name="Empty", created_by=user)
    membership = WorkspaceMembership.objects.create(
        workspace=ws, user=user, role=WorkspaceRole.MANAGE
    )
    return user, ws, membership


@pytest.mark.django_db
@pytest.mark.parametrize("all_of", [True, False])
def test_member_of_a_workspace_with_no_sources_is_denied_and_logged(settings, caplog, all_of):
    settings.WORKSPACE_ACCESS_REQUIRES_EVERY_TENANT = all_of
    user, ws, _membership = _empty_workspace("nosrc@example.com")
    caplog.set_level(logging.ERROR, logger=LOGGER)

    result = resolve_workspace_access_ex(user, ws.id)

    assert not result.granted
    assert result.denied_reason == NO_SOURCES
    [record] = [r for r in caplog.records if r.getMessage().startswith(EVENT)]
    assert record.levelno == logging.ERROR
    assert record.workspace_id == str(ws.id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_async_gates_deny_a_workspace_with_no_sources():
    user, ws, membership = await sync_to_async(_empty_workspace)("nosrc-async@example.com")

    single = await aresolve_workspace_access_ex(user, ws.id)
    bulk = await aresolve_local_access_many(user, [membership])

    assert single.denied_reason == NO_SOURCES
    assert bulk[ws.id].denied_reason == NO_SOURCES


@pytest.mark.django_db
def test_manager_can_still_open_and_delete_a_workspace_with_no_sources():
    """The remediation exemption covers it, or nothing could remove it."""
    user, ws, _membership = _empty_workspace("nosrc-delete@example.com")
    client = Client()
    client.force_login(user)

    assert client.get(f"/api/workspaces/{ws.id}/").status_code == 200
    assert client.get(f"/api/workspaces/{ws.id}/threads/").status_code == 403
    assert client.delete(f"/api/workspaces/{ws.id}/").status_code == 204
    assert not Workspace.objects.filter(id=ws.id).exists()


@pytest.mark.django_db
def test_listing_agrees_with_the_gate_for_a_workspace_with_no_sources():
    user, ws, _membership = _empty_workspace("nosrc-list@example.com")
    client = Client()
    client.force_login(user)

    [entry] = [w for w in client.get("/api/workspaces/").json() if w["id"] == str(ws.id)]

    assert entry["has_access"] is False
