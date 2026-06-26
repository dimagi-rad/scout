"""Truthful-failure tests for thread-message loading during a checkpointer
outage (arch #256, finding 07#7).

``_load_thread_messages`` used to swallow ANY checkpointer error and return an
empty list with HTTP 200, so during a DB/checkpointer blip a user saw their
conversation apparently deleted with no error. The load must distinguish a
genuinely-empty thread (no checkpoint yet → 200 []) from a checkpointer failure
(→ a non-200 error the UI can surface as an error state).
"""

from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.test import AsyncClient

from apps.chat.helpers import CheckpointerUnavailable
from apps.chat.models import Thread
from apps.chat.thread_views import _load_thread_messages
from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)

User = get_user_model()


async def _make_owned_thread():
    user = await User.objects.acreate_user(email="ckpt-owner@b.c", password="x")
    ws = await Workspace.objects.acreate(name="W-ckpt", created_by=user)
    tenant = await Tenant.objects.acreate(
        external_id="t-ckpt", provider="commcare", canonical_name="Ckpt Tenant"
    )
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)
    tm = await TenantMembership.objects.acreate(user=user, tenant=tenant)  # noqa: F841
    await WorkspaceMembership.objects.acreate(
        workspace=ws, user=user, role=WorkspaceRole.READ_WRITE
    )
    thread = await Thread.objects.acreate(user=user, workspace=ws, title="t")
    return user, ws, thread


@pytest.mark.asyncio
async def test_load_thread_messages_raises_on_checkpointer_error():
    """A checkpointer failure must propagate as CheckpointerUnavailable, not be
    silently converted to []."""
    boom = AsyncMock(side_effect=RuntimeError("connection reset"))
    fake_ckpt = AsyncMock()
    fake_ckpt.aget_tuple = boom
    with patch(
        "apps.chat.thread_views.ensure_checkpointer",
        AsyncMock(return_value=fake_ckpt),
    ):
        with pytest.raises(CheckpointerUnavailable):
            await _load_thread_messages("11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_load_thread_messages_returns_empty_when_no_checkpoint():
    """A brand-new thread with no checkpoint yet is genuinely empty → []."""
    fake_ckpt = AsyncMock()
    fake_ckpt.aget_tuple = AsyncMock(return_value=None)
    with patch(
        "apps.chat.thread_views.ensure_checkpointer",
        AsyncMock(return_value=fake_ckpt),
    ):
        result = await _load_thread_messages("22222222-2222-2222-2222-222222222222")
    assert result == []


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_thread_messages_view_returns_503_on_checkpointer_outage():
    """The view must surface a non-200 status during a checkpointer blip so the
    frontend can show an error state instead of 'conversation deleted'."""
    user, ws, thread = await _make_owned_thread()

    client = AsyncClient()
    await client.alogin(email=user.email, password="x")

    with patch(
        "apps.chat.thread_views._load_thread_messages",
        AsyncMock(side_effect=CheckpointerUnavailable("blip")),
    ):
        resp = await client.get(f"/api/workspaces/{ws.id}/threads/{thread.id}/messages/")

    assert resp.status_code == 503
