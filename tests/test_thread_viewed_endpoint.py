import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient

from apps.chat.models import Thread
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)

User = get_user_model()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_viewed_sets_last_viewed_at():
    user = await sync_to_async(User.objects.create_user)(email="a@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W", created_by=user)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    client = AsyncClient()
    await sync_to_async(client.login)(email="a@b.c", password="x")

    resp = await client.post(f"/api/workspaces/{ws.id}/threads/{thread.id}/viewed/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    await sync_to_async(thread.refresh_from_db)()
    assert thread.last_viewed_at is not None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_viewed_returns_403_for_non_member():
    user = await sync_to_async(User.objects.create_user)(email="b@b.c", password="x")
    await sync_to_async(User.objects.create_user)(email="c@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W2", created_by=user)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)
    client = AsyncClient()
    await sync_to_async(client.login)(email="c@b.c", password="x")

    resp = await client.post(f"/api/workspaces/{ws.id}/threads/{thread.id}/viewed/")
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_viewed_returns_404_for_unknown_thread():
    user = await sync_to_async(User.objects.create_user)(email="d@b.c", password="x")
    ws = await sync_to_async(Workspace.objects.create)(name="W3", created_by=user)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    client = AsyncClient()
    await sync_to_async(client.login)(email="d@b.c", password="x")

    resp = await client.post(f"/api/workspaces/{ws.id}/threads/{uuid.uuid4()}/viewed/")
    assert resp.status_code == 404
