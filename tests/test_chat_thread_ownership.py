"""Tests for thread-ownership validation in the chat endpoint (Fix 1a)."""

import asyncio
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import AsyncClient

from apps.chat.models import Thread
from apps.chat.views import _upsert_thread
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
    await client.alogin(email=email, password=password)
    csrf_resp = await client.get("/api/auth/csrf/")
    csrf_token = csrf_resp.json()["csrfToken"]
    client.defaults["HTTP_X_CSRFTOKEN"] = csrf_token
    return client


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_rejects_foreign_thread_id():
    """A user cannot inject content into another user's thread by passing
    that thread's UUID in the request body."""
    owner = await User.objects.acreate_user(email="owner-oth@b.c", password="x")
    attacker = await User.objects.acreate_user(email="attacker-oth@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-attack", created_by=owner)
    tenant = await Tenant.objects.acreate(
        external_id="t-attack", provider="commcare", canonical_name="Attack Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=owner,
        role=WorkspaceRole.READ_WRITE,
    )
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=attacker,
        role=WorkspaceRole.READ_WRITE,
    )
    owners_thread = await Thread.objects.acreate(
        workspace=ws,
        user=owner,
    )

    client = await _csrf_client("attacker-oth@b.c", "x")
    resp = await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [{"role": "user", "content": "inject content"}],
                "workspaceId": str(ws.id),
                "threadId": str(owners_thread.id),
            }
        ),
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
    user = await User.objects.acreate_user(email="own-thread@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-own", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-own", provider="commcare", canonical_name="Own Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    await WorkspaceMembership.objects.acreate(
        workspace=ws,
        user=user,
        role=WorkspaceRole.READ_WRITE,
    )
    own_thread = await Thread.objects.acreate(workspace=ws, user=user)

    client = await _csrf_client("own-thread@b.c", "x")
    # This will fail at the MCP/agent layer (no credentials), but must NOT 404
    # on the ownership check.
    resp = await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "hello"},
                ],
                "workspaceId": str(ws.id),
                "threadId": str(own_thread.id),
            }
        ),
        content_type="application/json",
    )
    # Must NOT be 404 from thread-ownership check
    assert resp.status_code != 404


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_upsert_thread_bumps_updated_at_on_subsequent_turn():
    """Finding #5: every chat turn must advance Thread.updated_at. Previously
    aupdate_or_create with only create_defaults ran save(update_fields=set())
    on the existing-row path, which silently bypassed auto_now and left
    updated_at frozen at thread creation — breaking sidebar ordering and
    the "newer than last_viewed" green-dot indicator."""
    user = await User.objects.acreate_user(
        email="updated-at@b.c",
        password="x",
    )
    ws = await Workspace.objects.acreate(name="W-updated", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-up",
        provider="commcare",
        canonical_name="Up Tenant",
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)

    thread_id = "11111111-1111-1111-1111-111111111111"
    await _upsert_thread(thread_id, user, "first turn", workspace=ws)
    initial = await Thread.objects.aget(id=thread_id)
    initial_updated_at = initial.updated_at

    # Postgres timestamps have microsecond precision; sleep a beat so the
    # bump is observable.
    await asyncio.sleep(0.01)
    await _upsert_thread(thread_id, user, "second turn", workspace=ws)
    refreshed = await Thread.objects.aget(id=thread_id)
    assert refreshed.updated_at > initial_updated_at, (
        f"updated_at should bump on second turn; "
        f"initial={initial_updated_at!r} refreshed={refreshed.updated_at!r}"
    )
