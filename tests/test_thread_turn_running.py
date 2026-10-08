"""``turn_running``: the thread list and messages report a thread whose turn lease is live,
so a chat opened away from the tab running its turn waits for it (#856)."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from django.test import AsyncClient
from django.utils import timezone

from apps.chat.models import Thread
from apps.chat.turn_lease import aacquire_turn_lease

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _client(user):
    client = AsyncClient()
    await client.alogin(email=user.email, password="testpass123")
    return client


async def _messages(client, workspace, thread_id):
    with patch("apps.chat.thread_views._load_thread_messages", AsyncMock(return_value=[])):
        response = await client.get(
            f"/api/workspaces/{workspace.id}/threads/{thread_id}/messages/?include=pending"
        )
    assert response.status_code == 200
    return response.json()


async def _listed(client, workspace, thread_id):
    response = await client.get(f"/api/workspaces/{workspace.id}/threads/")
    assert response.status_code == 200
    return next(row for row in response.json() if row["id"] == str(thread_id))


async def test_idle_thread_is_not_running(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace, title="t")
    client = await _client(user)

    assert (await _messages(client, workspace, thread.id))["turn_running"] is False
    assert (await _listed(client, workspace, thread.id))["turn_running"] is False


async def test_held_lease_reads_as_running_until_released(user, workspace):
    thread = await Thread.objects.acreate(user=user, workspace=workspace, title="t")
    client = await _client(user)
    lease = await aacquire_turn_lease(thread.id)
    assert lease is not None

    assert (await _messages(client, workspace, thread.id))["turn_running"] is True
    assert (await _listed(client, workspace, thread.id))["turn_running"] is True
    detail = await client.get(f"/api/workspaces/{workspace.id}/threads/{thread.id}/")
    assert detail.json()["turn_running"] is True

    await lease.release()

    assert (await _messages(client, workspace, thread.id))["turn_running"] is False
    assert (await _listed(client, workspace, thread.id))["turn_running"] is False


async def test_lapsed_lease_of_a_dead_holder_is_not_running(user, workspace):
    thread = await Thread.objects.acreate(
        user=user,
        workspace=workspace,
        title="t",
        turn_lease_token=uuid.uuid4(),
        turn_lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    client = await _client(user)

    assert (await _messages(client, workspace, thread.id))["turn_running"] is False
    assert (await _listed(client, workspace, thread.id))["turn_running"] is False


async def test_thread_without_a_row_is_not_running(user, workspace):
    client = await _client(user)

    body = await _messages(client, workspace, uuid.uuid4())

    assert body == {"messages": [], "pending_request": None, "turn_running": False}
