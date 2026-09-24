from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from tests.tenant_access import usable_connection


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
    TenantMembership.objects.create(
        user=user, tenant=t1, connection=usable_connection(user, t1.provider)
    )
    TenantMembership.objects.create(
        user=user, tenant=t2, connection=usable_connection(user, t2.provider)
    )
    ws = Workspace.objects.create(name="Smoke WS", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
    WorkspaceTenant.objects.create(workspace=ws, tenant=t1)
    return user, ws, t2


@pytest.mark.django_db(transaction=True)
def test_adding_a_tenant_that_already_serves_data_only_rebuilds_views(api_client, setup):
    user, ws, t2 = setup
    TenantSchema.objects.create(tenant=t2, schema_name="smoke_2_live", state=SchemaState.ACTIVE)

    with (
        patch(
            "apps.workspaces.services.workspace_service.rebuild_workspace_view_schema.defer"
        ) as mock_defer,
        patch("apps.workspaces.services.workspace_service.materialize_workspace.defer") as load,
    ):
        api_client.force_login(user)
        resp = api_client.post(
            f"/api/workspaces/{ws.id}/tenants/",
            {"tenant_id": str(t2.id)},
            format="json",
        )

    assert resp.status_code == 202
    assert WorkspaceTenant.objects.filter(workspace=ws, tenant=t2).exists()
    mock_defer.assert_called_once_with(workspace_id=str(ws.id))
    load.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_adding_an_unloaded_tenant_loads_it_before_publishing(api_client, setup):
    """A new source is loaded first; the load then publishes the views and Cube,
    so it is never published as a missing source."""
    user, ws, t2 = setup

    with (
        patch(
            "apps.workspaces.services.workspace_service.rebuild_workspace_view_schema.defer"
        ) as rebuild,
        patch("apps.workspaces.services.workspace_service.materialize_workspace.defer") as load,
    ):
        api_client.force_login(user)
        resp = api_client.post(
            f"/api/workspaces/{ws.id}/tenants/",
            {"tenant_id": str(t2.id)},
            format="json",
        )

    assert resp.status_code == 202
    rebuild.assert_not_called()
    load.assert_called_once()
    kwargs = load.call_args.kwargs
    assert kwargs["workspace_id"] == str(ws.id)
    assert kwargs["user_id"] == str(user.id)
    assert kwargs["only_unserved"] is True
    assert kwargs["load_intent"] == {str(t2.id): 1}
