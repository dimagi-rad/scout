"""Tests for thread-ownership validation in the chat endpoint (Fix 1a)."""

import json

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient

from apps.chat.models import Thread
from apps.users.models import Tenant
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)

User = get_user_model()


async def _csrf_client(email, password):
    """Return an AsyncClient with a valid CSRF token already set."""
    client = AsyncClient(enforce_csrf_checks=True)
    await sync_to_async(client.login)(email=email, password=password)
    csrf_resp = await client.get("/api/auth/csrf/")
    csrf_token = csrf_resp.json()["csrfToken"]
    client.defaults["HTTP_X_CSRFTOKEN"] = csrf_token
    return client


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_rejects_foreign_thread_id():
    """A user cannot inject content into another user's thread by passing
    that thread's UUID in the request body."""
    owner = await sync_to_async(User.objects.create_user)(
        email="owner-oth@b.c", password="x"
    )
    attacker = await sync_to_async(User.objects.create_user)(
        email="attacker-oth@b.c", password="x"
    )
    ws = await sync_to_async(Workspace.objects.create)(name="W-attack", created_by=owner)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-attack", provider="commcare", canonical_name="Attack Tenant"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=owner, role=WorkspaceRole.READ_WRITE,
    )
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=attacker, role=WorkspaceRole.READ_WRITE,
    )
    owners_thread = await sync_to_async(Thread.objects.create)(
        workspace=ws, user=owner,
    )

    client = await _csrf_client("attacker-oth@b.c", "x")
    resp = await client.post(
        "/api/chat/",
        data=json.dumps({
            "messages": [{"role": "user", "content": "inject content"}],
            "workspaceId": str(ws.id),
            "threadId": str(owners_thread.id),
        }),
        content_type="application/json",
    )
    # Must be rejected — 404 hides thread existence, 403 from earlier guards also acceptable
    assert resp.status_code in (403, 404)
    # Specifically, if it reaches the ownership check, it should be 404
    if resp.status_code == 404:
        assert b"Thread not found" in resp.content


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_allows_own_thread_id():
    """A user can attach a turn to their own thread without rejection."""
    user = await sync_to_async(User.objects.create_user)(
        email="own-thread@b.c", password="x"
    )
    ws = await sync_to_async(Workspace.objects.create)(name="W-own", created_by=user)
    tenant = await sync_to_async(Tenant.objects.create)(
        external_id="t-own", provider="commcare", canonical_name="Own Tenant"
    )
    await sync_to_async(WorkspaceTenant.objects.create)(workspace=ws, tenant=tenant)
    await sync_to_async(WorkspaceMembership.objects.create)(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE,
    )
    own_thread = await sync_to_async(Thread.objects.create)(workspace=ws, user=user)

    client = await _csrf_client("own-thread@b.c", "x")
    # This will fail at the MCP/agent layer (no credentials), but must NOT 404
    # on the ownership check.
    resp = await client.post(
        "/api/chat/",
        data=json.dumps({
            "messages": [{"role": "user", "content": "hello"},],
            "workspaceId": str(ws.id),
            "threadId": str(own_thread.id),
        }),
        content_type="application/json",
    )
    # Must NOT be 404 from thread-ownership check
    assert resp.status_code != 404
