from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.workspaces.models import (
    SchemaState,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceViewSchema,
)


@pytest.fixture
def workspace_with_view_schema(transactional_db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(email="ttl@example.com", password="pass")
    ws = Workspace.objects.create(name="TTL WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    vs = WorkspaceViewSchema.objects.create(
        workspace=ws,
        schema_name="ws_ttltest123456ab",
        state=SchemaState.ACTIVE,
    )
    return ws, vs


def test_expire_inactive_schemas_also_expires_stale_view_schemas(workspace_with_view_schema):
    from apps.workspaces.tasks import expire_inactive_schemas

    _ws, vs = workspace_with_view_schema
    vs.last_accessed_at = timezone.now() - timedelta(hours=25)
    vs.save()

    with patch("apps.workspaces.tasks.teardown_view_schema_task") as mock_teardown:
        expire_inactive_schemas()

    vs.refresh_from_db()
    assert vs.state == SchemaState.TEARDOWN
    mock_teardown.delay_on_commit.assert_called_once_with(str(vs.id))


def test_recently_accessed_view_schema_not_expired(workspace_with_view_schema):
    from apps.workspaces.tasks import expire_inactive_schemas

    _ws, vs = workspace_with_view_schema
    vs.last_accessed_at = timezone.now() - timedelta(hours=1)
    vs.save()

    expire_inactive_schemas()

    vs.refresh_from_db()
    assert vs.state == SchemaState.ACTIVE


def test_view_schema_with_null_last_accessed_not_expired(workspace_with_view_schema):
    from apps.workspaces.tasks import expire_inactive_schemas

    _ws, vs = workspace_with_view_schema
    vs.last_accessed_at = None
    vs.save()

    expire_inactive_schemas()

    vs.refresh_from_db()
    assert vs.state == SchemaState.ACTIVE
