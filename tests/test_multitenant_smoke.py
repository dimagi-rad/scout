from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import Workspace, WorkspaceMembership, WorkspaceRole, WorkspaceTenant


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def setup(transactional_db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(email="smoke@example.com", password="pass")
    t1 = Tenant.objects.create(provider="commcare", external_id="smoke-1", canonical_name="Smoke 1")
    t2 = Tenant.objects.create(provider="commcare", external_id="smoke-2", canonical_name="Smoke 2")
    TenantMembership.objects.create(user=user, tenant=t1)
    TenantMembership.objects.create(user=user, tenant=t2)
    ws = Workspace.objects.create(name="Smoke WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    return user, ws, t2


@pytest.mark.django_db(transaction=True)
def test_adding_tenant_dispatches_rebuild_task(api_client, setup):
    user, ws, t2 = setup

    with patch(
        "apps.workspaces.services.workspace_service.rebuild_workspace_view_schema.defer"
    ) as mock_defer:
        api_client.force_login(user)
        resp = api_client.post(
            f"/api/workspaces/{ws.id}/tenants/",
            {"tenant_id": str(t2.id)},
            format="json",
        )

    assert resp.status_code == 202
    assert WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()
    mock_defer.assert_called_once_with(workspace_id=str(ws.id))
