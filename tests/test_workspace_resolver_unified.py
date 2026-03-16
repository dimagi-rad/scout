"""Tests for unified workspace resolution functions."""

import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.http import JsonResponse

from apps.projects.models import Workspace, WorkspaceMembership, WorkspaceRole

User = get_user_model()


@pytest.mark.django_db
class TestResolveWorkspaceRaw:
    """Tests for sync non-DRF workspace resolution."""

    def test_returns_workspace_on_valid_membership(self, user, workspace):
        from apps.projects.workspace_resolver import resolve_workspace_raw

        ws, err = resolve_workspace_raw(user, workspace.id)
        assert ws is not None
        assert ws.id == workspace.id
        assert err is None

    def test_returns_error_on_missing_membership(self, user):
        from apps.projects.workspace_resolver import resolve_workspace_raw

        ws, err = resolve_workspace_raw(user, uuid.uuid4())
        assert ws is None
        assert isinstance(err, JsonResponse)
        assert err.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db
class TestAresolveWorkspace:
    """Tests for async workspace resolution."""

    async def test_returns_workspace_on_valid_membership(self):
        from apps.projects.workspace_resolver import aresolve_workspace

        user = await sync_to_async(User.objects.create_user)(
            email="async-resolve@example.com", password="pass"
        )
        ws = await Workspace.objects.acreate(name="Async WS", created_by=user)
        await WorkspaceMembership.objects.acreate(
            workspace=ws, user=user, role=WorkspaceRole.MANAGE
        )

        result, err = await aresolve_workspace(user, ws.id)
        assert result is not None
        assert result.id == ws.id
        assert err is None

    async def test_returns_error_on_missing_membership(self):
        from apps.projects.workspace_resolver import aresolve_workspace

        user = await sync_to_async(User.objects.create_user)(
            email="async-resolve-denied@example.com", password="pass"
        )

        result, err = await aresolve_workspace(user, uuid.uuid4())
        assert result is None
        assert isinstance(err, JsonResponse)
        assert err.status_code == 403
