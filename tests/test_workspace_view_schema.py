import pytest

from apps.users.models import Tenant
from apps.workspaces.models import SchemaState, Workspace, WorkspaceTenant, WorkspaceViewSchema


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(
        provider="commcare", external_id="test-domain", canonical_name="Test Domain"
    )


@pytest.fixture
def tenant2(db):
    return Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other Domain"
    )


@pytest.fixture
def workspace(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(email="user@example.com", password="pass")
    from apps.workspaces.models import WorkspaceMembership, WorkspaceRole

    ws = Workspace.objects.create(name="Multi-Tenant WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    return ws


def test_workspace_view_schema_can_be_created(workspace):
    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_abc123",
        state=SchemaState.PROVISIONING,
    )
    assert vs.id is not None
    assert vs.schema_name == "ws_abc123"
    assert vs.state == SchemaState.PROVISIONING
    assert vs.last_accessed_at is None


def test_workspace_view_schema_touch_updates_last_accessed_at(workspace):
    import datetime

    import freezegun

    vs = WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_touch_test",
        state=SchemaState.ACTIVE,
    )
    with freezegun.freeze_time("2026-01-01 12:00:00"):
        vs.touch()
    vs.refresh_from_db()
    assert vs.last_accessed_at == datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def test_workspace_view_schema_is_one_to_one(workspace):
    from django.db import IntegrityError

    WorkspaceViewSchema.objects.create(
        workspace=workspace,
        schema_name="ws_first",
        state=SchemaState.PROVISIONING,
    )
    with pytest.raises(IntegrityError):
        WorkspaceViewSchema.objects.create(
            workspace=workspace,
            schema_name="ws_second",
            state=SchemaState.PROVISIONING,
        )


def test_workspace_tenant_has_uuid_pk(workspace, tenant):
    """WorkspaceTenant must have a UUID primary key (Amendment A)."""
    import uuid

    wt = WorkspaceTenant.objects.create(workspace=workspace, tenant=tenant)
    assert isinstance(wt.id, uuid.UUID)


def test_workspace_view_schema_has_uuid_pk(workspace):
    """WorkspaceViewSchema must have a UUID primary key matching other models."""
    import uuid as uuid_module

    vs = WorkspaceViewSchema.objects.create(workspace=workspace, schema_name="ws_aabbccdd11223344")
    assert isinstance(vs.pk, uuid_module.UUID)
