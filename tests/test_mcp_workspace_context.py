from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from apps.workspaces.models import SchemaState, Workspace, WorkspaceTenant, WorkspaceViewSchema


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_load_workspace_context_single_tenant_delegates_to_tenant_context():
    from django.contrib.auth import get_user_model

    from apps.users.models import Tenant
    from apps.workspaces.models import WorkspaceMembership, WorkspaceRole

    User = get_user_model()
    user = await sync_to_async(User.objects.create_user)(email="ctx@example.com", password="pass")
    tenant = await Tenant.objects.acreate(
        provider="commcare", external_id="ctx-domain", canonical_name="CTX"
    )
    ws = await Workspace.objects.acreate(name="CTX WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=tenant)

    with patch("mcp_server.context.load_tenant_context", new_callable=AsyncMock) as mock_ltc:
        mock_ltc.return_value = MagicMock(schema_name="ctx_domain")
        from mcp_server.context import load_workspace_context

        result = await load_workspace_context(str(ws.id))

    mock_ltc.assert_called_once_with("ctx-domain")
    assert result.schema_name == "ctx_domain"


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_load_workspace_context_multi_tenant_uses_view_schema():
    from django.conf import settings
    from django.contrib.auth import get_user_model

    from apps.users.models import Tenant
    from apps.workspaces.models import WorkspaceMembership, WorkspaceRole

    User = get_user_model()
    user = await sync_to_async(User.objects.create_user)(email="ctx2@example.com", password="pass")
    t1 = await Tenant.objects.acreate(
        provider="commcare", external_id="ctx-d1", canonical_name="D1"
    )
    t2 = await Tenant.objects.acreate(
        provider="commcare", external_id="ctx-d2", canonical_name="D2"
    )
    ws = await Workspace.objects.acreate(name="Multi CTX WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)
    await WorkspaceViewSchema.objects.acreate(
        workspace=ws,
        schema_name="ws_multitest123456",
        state=SchemaState.ACTIVE,
    )

    with patch.object(settings, "MANAGED_DATABASE_URL", "postgresql://test/db"):
        with patch("mcp_server.context._parse_db_url") as mock_parse:
            mock_parse.return_value = {"host": "localhost", "dbname": "db"}
            from mcp_server.context import load_workspace_context

            ctx = await load_workspace_context(str(ws.id))

    assert ctx.schema_name == "ws_multitest123456"
    assert ctx.tenant_id == str(ws.id)


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_load_workspace_context_multi_tenant_raises_if_no_active_view_schema():
    from django.contrib.auth import get_user_model

    from apps.users.models import Tenant
    from apps.workspaces.models import WorkspaceMembership, WorkspaceRole

    User = get_user_model()
    user = await sync_to_async(User.objects.create_user)(email="ctx3@example.com", password="pass")
    t1 = await Tenant.objects.acreate(
        provider="commcare", external_id="ctx-noview-1", canonical_name="NV1"
    )
    t2 = await Tenant.objects.acreate(
        provider="commcare", external_id="ctx-noview-2", canonical_name="NV2"
    )
    ws = await Workspace.objects.acreate(name="NoView WS", created_by=user)
    await WorkspaceMembership.objects.acreate(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t1)
    await WorkspaceTenant.objects.acreate(workspace=ws, tenant=t2)

    from mcp_server.context import load_workspace_context

    with pytest.raises(ValueError, match="No active view schema"):
        await load_workspace_context(str(ws.id))
