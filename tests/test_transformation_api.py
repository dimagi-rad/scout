"""Tests for the transformation REST API (Milestone 7)."""

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.transformations.models import (
    TransformationAsset,
    TransformationRun,
    TransformationScope,
)


@pytest.fixture
def api_client():
    return APIClient()


# ---------------------------------------------------------------------------
# Asset list & filter tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_list_assets_returns_visible_assets(api_client, user, tenant, tenant_membership, workspace):
    """Authenticated user sees assets for their tenants and workspaces."""
    asset_t = TransformationAsset.objects.create(
        name="tenant_asset",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    asset_w = TransformationAsset.objects.create(
        name="workspace_asset",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        sql_content="SELECT 2",
    )
    api_client.force_login(user)
    resp = api_client.get("/api/transformations/assets/")
    assert resp.status_code == 200
    ids = {str(a["id"]) for a in resp.data}
    assert str(asset_t.id) in ids
    assert str(asset_w.id) in ids


@pytest.mark.django_db
def test_list_assets_scope_filter(api_client, user, tenant, tenant_membership, workspace):
    TransformationAsset.objects.create(
        name="t_asset",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    TransformationAsset.objects.create(
        name="w_asset",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        sql_content="SELECT 2",
    )
    api_client.force_login(user)
    resp = api_client.get("/api/transformations/assets/", {"scope": "tenant"})
    assert resp.status_code == 200
    assert all(a["scope"] == "tenant" for a in resp.data)


@pytest.mark.django_db
def test_list_assets_tenant_id_filter(api_client, user, tenant, tenant_membership):
    TransformationAsset.objects.create(
        name="t_asset",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    api_client.force_login(user)
    resp = api_client.get("/api/transformations/assets/", {"tenant_id": str(tenant.id)})
    assert resp.status_code == 200
    assert len(resp.data) == 1


@pytest.mark.django_db
def test_list_assets_workspace_id_filter(api_client, user, tenant_membership, workspace):
    TransformationAsset.objects.create(
        name="w_asset",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        sql_content="SELECT 1",
    )
    api_client.force_login(user)
    resp = api_client.get("/api/transformations/assets/", {"workspace_id": str(workspace.id)})
    assert resp.status_code == 200
    assert len(resp.data) == 1


# ---------------------------------------------------------------------------
# Create asset tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_tenant_asset(api_client, user, tenant, tenant_membership):
    api_client.force_login(user)
    resp = api_client.post(
        "/api/transformations/assets/",
        {
            "name": "my_tenant_asset",
            "scope": "tenant",
            "tenant": str(tenant.id),
            "sql_content": "SELECT * FROM raw_cases",
        },
        format="json",
    )
    assert resp.status_code == 201
    assert resp.data["name"] == "my_tenant_asset"
    assert resp.data["created_by"] == user.id


@pytest.mark.django_db
def test_create_workspace_asset_with_write_role(api_client, write_user, workspace):
    api_client.force_login(write_user)
    resp = api_client.post(
        "/api/transformations/assets/",
        {
            "name": "ws_asset",
            "scope": "workspace",
            "workspace": str(workspace.id),
            "sql_content": "SELECT 1",
        },
        format="json",
    )
    assert resp.status_code == 201


@pytest.mark.django_db
def test_create_system_asset_forbidden(api_client, user, tenant, tenant_membership):
    api_client.force_login(user)
    resp = api_client.post(
        "/api/transformations/assets/",
        {
            "name": "sys_asset",
            "scope": "system",
            "tenant": str(tenant.id),
            "sql_content": "SELECT 1",
        },
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_create_workspace_asset_read_role_forbidden(api_client, read_user, workspace):
    """User with read role cannot create workspace assets."""
    api_client.force_login(read_user)
    resp = api_client.post(
        "/api/transformations/assets/",
        {
            "name": "ws_asset",
            "scope": "workspace",
            "workspace": str(workspace.id),
            "sql_content": "SELECT 1",
        },
        format="json",
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Create asset - cross-tenant/workspace authorization tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_tenant_asset_for_foreign_tenant_forbidden(api_client, user, tenant_membership):
    """User cannot create a tenant-scoped asset for a tenant they don't belong to."""
    from apps.users.models import Tenant

    foreign_tenant = Tenant.objects.create(
        provider="commcare", external_id="foreign-domain", canonical_name="Foreign"
    )
    api_client.force_login(user)
    resp = api_client.post(
        "/api/transformations/assets/",
        {
            "name": "foreign_asset",
            "scope": "tenant",
            "tenant": str(foreign_tenant.id),
            "sql_content": "SELECT 1",
        },
        format="json",
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Update asset tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_tenant_asset(api_client, user, tenant, tenant_membership):
    asset = TransformationAsset.objects.create(
        name="updatable",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    api_client.force_login(user)
    resp = api_client.patch(
        f"/api/transformations/assets/{asset.id}/",
        {"sql_content": "SELECT 2"},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.data["sql_content"] == "SELECT 2"


@pytest.mark.django_db
def test_update_system_asset_forbidden(api_client, user, tenant, tenant_membership):
    asset = TransformationAsset.objects.create(
        name="sys_immutable",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    api_client.force_login(user)
    resp = api_client.patch(
        f"/api/transformations/assets/{asset.id}/",
        {"sql_content": "SELECT 2"},
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_update_workspace_asset_read_role_forbidden(api_client, read_user, workspace):
    """User with read role cannot update workspace assets."""
    asset = TransformationAsset.objects.create(
        name="ws_readonly",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        sql_content="SELECT 1",
    )
    api_client.force_login(read_user)
    resp = api_client.patch(
        f"/api/transformations/assets/{asset.id}/",
        {"sql_content": "SELECT 2"},
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_update_cannot_reassign_container(api_client, user, tenant, tenant_membership, workspace):
    """PATCH cannot change scope, tenant, or workspace (immutable after creation)."""
    from apps.users.models import Tenant

    asset = TransformationAsset.objects.create(
        name="locked_asset",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    foreign_tenant = Tenant.objects.create(
        provider="commcare", external_id="foreign", canonical_name="Foreign"
    )
    api_client.force_login(user)
    resp = api_client.patch(
        f"/api/transformations/assets/{asset.id}/",
        {"tenant": str(foreign_tenant.id)},
        format="json",
    )
    assert resp.status_code == 200
    asset.refresh_from_db()
    # tenant should not have changed — field is read-only on update
    assert asset.tenant_id == tenant.id


# ---------------------------------------------------------------------------
# Delete asset tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_tenant_asset(api_client, user, tenant, tenant_membership):
    asset = TransformationAsset.objects.create(
        name="deletable",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    api_client.force_login(user)
    resp = api_client.delete(f"/api/transformations/assets/{asset.id}/")
    assert resp.status_code == 204


@pytest.mark.django_db
def test_delete_system_asset_forbidden(api_client, user, tenant, tenant_membership):
    asset = TransformationAsset.objects.create(
        name="sys_nodelete",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT 1",
    )
    api_client.force_login(user)
    resp = api_client.delete(f"/api/transformations/assets/{asset.id}/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_delete_workspace_asset_read_role_forbidden(api_client, read_user, workspace):
    """User with read role cannot delete workspace assets."""
    asset = TransformationAsset.objects.create(
        name="ws_nodelete",
        scope=TransformationScope.WORKSPACE,
        workspace=workspace,
        sql_content="SELECT 1",
    )
    api_client.force_login(read_user)
    resp = api_client.delete(f"/api/transformations/assets/{asset.id}/")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Lineage endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lineage_endpoint(api_client, user, tenant, tenant_membership):
    asset_a = TransformationAsset.objects.create(
        name="stg_case_patient",
        scope=TransformationScope.SYSTEM,
        tenant=tenant,
        sql_content="SELECT * FROM raw_cases",
        description="System staging",
    )
    asset_b = TransformationAsset.objects.create(
        name="cases_clean",
        scope=TransformationScope.TENANT,
        tenant=tenant,
        sql_content="SELECT * FROM stg_case_patient",
        replaces=asset_a,
        description="Tenant override",
    )
    api_client.force_login(user)
    resp = api_client.get(f"/api/transformations/assets/{asset_b.id}/lineage/")
    assert resp.status_code == 200
    names = [entry["name"] for entry in resp.data]
    assert "cases_clean" in names
    assert "stg_case_patient" in names


# ---------------------------------------------------------------------------
# Run list tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_list_runs(api_client, user, tenant, tenant_membership):
    run = TransformationRun.objects.create(tenant=tenant, status="completed")
    api_client.force_login(user)
    resp = api_client.get("/api/transformations/runs/")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.data]
    assert str(run.id) in ids


@pytest.mark.django_db
def test_list_runs_tenant_filter(api_client, user, tenant, tenant_membership):
    from apps.users.models import Tenant

    other_tenant = Tenant.objects.create(
        provider="commcare", external_id="other-domain", canonical_name="Other"
    )
    TransformationRun.objects.create(tenant=tenant, status="completed")
    TransformationRun.objects.create(tenant=other_tenant, status="completed")
    api_client.force_login(user)
    resp = api_client.get("/api/transformations/runs/", {"tenant_id": str(tenant.id)})
    assert resp.status_code == 200
    assert len(resp.data) == 1
    assert str(resp.data[0]["tenant"]) == str(tenant.id)


# ---------------------------------------------------------------------------
# Trigger endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_trigger_run(api_client, user, tenant, tenant_membership):
    from apps.workspaces.models import TenantSchema

    TenantSchema.objects.create(tenant=tenant, schema_name="test_schema", state="active")

    mock_run = TransformationRun.objects.create(tenant=tenant, status="completed")

    api_client.force_login(user)
    with patch(
        "apps.transformations.services.executor.run_transformation_pipeline",
        return_value=mock_run,
    ):
        resp = api_client.post(
            "/api/transformations/runs/trigger/",
            {"tenant_id": str(tenant.id)},
            format="json",
        )
    assert resp.status_code == 201
    assert resp.data["status"] == "completed"


@pytest.mark.django_db
def test_trigger_foreign_tenant_forbidden(api_client, user, tenant_membership):
    """User cannot trigger a run for a tenant they don't belong to."""
    from apps.users.models import Tenant

    foreign_tenant = Tenant.objects.create(
        provider="commcare", external_id="foreign-domain", canonical_name="Foreign"
    )
    api_client.force_login(user)
    resp = api_client.post(
        "/api/transformations/runs/trigger/",
        {"tenant_id": str(foreign_tenant.id)},
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_trigger_without_tenant_id(api_client, user, tenant_membership):
    api_client.force_login(user)
    resp = api_client.post("/api/transformations/runs/trigger/", {}, format="json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_trigger_foreign_workspace_forbidden(api_client, user, tenant, tenant_membership):
    """User cannot trigger a run with a workspace they don't belong to."""
    from apps.workspaces.models import TenantSchema, Workspace

    TenantSchema.objects.create(tenant=tenant, schema_name="test_schema", state="active")
    foreign_ws = Workspace.objects.create(name="Foreign WS")

    api_client.force_login(user)
    resp = api_client.post(
        "/api/transformations/runs/trigger/",
        {"tenant_id": str(tenant.id), "workspace_id": str(foreign_ws.id)},
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_trigger_workspace_read_role_forbidden(api_client, read_user, tenant, workspace):
    """User with read-only workspace role cannot trigger a run with that workspace."""
    from apps.users.models import TenantMembership
    from apps.workspaces.models import TenantSchema

    TenantMembership.objects.create(user=read_user, tenant=tenant)
    TenantSchema.objects.create(tenant=tenant, schema_name="test_schema", state="active")

    api_client.force_login(read_user)
    resp = api_client.post(
        "/api/transformations/runs/trigger/",
        {"tenant_id": str(tenant.id), "workspace_id": str(workspace.id)},
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_trigger_without_active_schema(api_client, user, tenant, tenant_membership):
    api_client.force_login(user)
    resp = api_client.post(
        "/api/transformations/runs/trigger/",
        {"tenant_id": str(tenant.id)},
        format="json",
    )
    assert resp.status_code == 400
    assert "No active schema" in resp.data["error"]


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unauthenticated_assets_forbidden(api_client):
    resp = api_client.get("/api/transformations/assets/")
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_unauthenticated_runs_forbidden(api_client):
    resp = api_client.get("/api/transformations/runs/")
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_unauthenticated_trigger_forbidden(api_client):
    resp = api_client.post("/api/transformations/runs/trigger/", {}, format="json")
    assert resp.status_code in (401, 403)
