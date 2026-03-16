"""Tests for CSRF enforcement on the chat streaming endpoint."""

import json

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_post_without_csrf_token_is_rejected():
    """POST /api/chat/ without a CSRF token must return 403."""
    client = AsyncClient(enforce_csrf_checks=True)

    User = get_user_model()
    await sync_to_async(User.objects.create_user)(email="csrf@example.com", password="pass")

    await sync_to_async(client.login)(email="csrf@example.com", password="pass")

    response = await client.post(
        "/api/chat/",
        data=json.dumps({"messages": [{"content": "hello"}], "workspaceId": "fake"}),
        content_type="application/json",
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_chat_post_with_csrf_token_is_accepted():
    """POST /api/chat/ with a valid CSRF token passes CSRF check (may fail on auth/workspace)."""
    client = AsyncClient(enforce_csrf_checks=True)

    User = get_user_model()
    await sync_to_async(User.objects.create_user)(email="csrf2@example.com", password="pass")
    await sync_to_async(client.login)(email="csrf2@example.com", password="pass")

    csrf_resp = await client.get("/api/auth/csrf/")
    csrf_token = csrf_resp.json()["csrfToken"]

    response = await client.post(
        "/api/chat/",
        data=json.dumps(
            {
                "messages": [{"content": "hello"}],
                "workspaceId": "00000000-0000-0000-0000-000000000001",
            }
        ),
        content_type="application/json",
        headers={"X-CSRFToken": csrf_token},
    )
    # Should NOT be 403 CSRF — expect 400 or 403 workspace access denied
    assert response.status_code != 403 or b"CSRF" not in response.content
