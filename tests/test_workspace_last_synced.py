"""Tests for the last_synced_at field on workspace endpoints."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.users.models import Tenant
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)


@pytest.fixture
def client():
    return Client(enforce_csrf_checks=False)


@pytest.fixture
def tenant_schema(db, tenant):
    return TenantSchema.objects.create(
        tenant=tenant, schema_name="test_schema", state=SchemaState.ACTIVE
    )


def _make_run(schema, state, completed_at):
    run = MaterializationRun.objects.create(tenant_schema=schema, pipeline="commcare", state=state)
    run.completed_at = completed_at
    run.save(update_fields=["completed_at"])
    return run


@pytest.mark.django_db
def test_list_returns_null_when_no_completed_runs(client, user, workspace):
    client.force_login(user)
    resp = client.get("/api/workspaces/")
    assert resp.status_code == 200
    entry = next(w for w in resp.json() if w["id"] == str(workspace.id))
    assert entry["last_synced_at"] is None


@pytest.mark.django_db
def test_list_returns_latest_completed_run(client, user, workspace, tenant_schema):
    now = timezone.now()
    older = now - timedelta(hours=2)
    _make_run(tenant_schema, MaterializationRun.RunState.COMPLETED, older)
    latest = _make_run(tenant_schema, MaterializationRun.RunState.COMPLETED, now)

    client.force_login(user)
    resp = client.get("/api/workspaces/")
    entry = next(w for w in resp.json() if w["id"] == str(workspace.id))
    assert entry["last_synced_at"] == latest.completed_at.isoformat()


@pytest.mark.django_db
def test_list_ignores_in_flight_and_failed_runs(client, user, workspace, tenant_schema):
    now = timezone.now()
    completed = _make_run(
        tenant_schema, MaterializationRun.RunState.COMPLETED, now - timedelta(hours=1)
    )
    _make_run(tenant_schema, MaterializationRun.RunState.LOADING, now)
    _make_run(tenant_schema, MaterializationRun.RunState.FAILED, now)

    client.force_login(user)
    resp = client.get("/api/workspaces/")
    entry = next(w for w in resp.json() if w["id"] == str(workspace.id))
    assert entry["last_synced_at"] == completed.completed_at.isoformat()


@pytest.mark.django_db
def test_list_multi_tenant_returns_max_across_tenants(client, user, workspace, tenant_schema):
    # Add a second tenant with a more-recent completed run
    second = Tenant.objects.create(
        provider="commcare", external_id="second-domain", canonical_name="Second"
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=second)
    second_schema = TenantSchema.objects.create(
        tenant=second, schema_name="second_schema", state=SchemaState.ACTIVE
    )

    now = timezone.now()
    _make_run(
        tenant_schema,
        MaterializationRun.RunState.COMPLETED,
        now - timedelta(hours=3),
    )
    latest = _make_run(second_schema, MaterializationRun.RunState.COMPLETED, now)

    client.force_login(user)
    resp = client.get("/api/workspaces/")
    entry = next(w for w in resp.json() if w["id"] == str(workspace.id))
    assert entry["last_synced_at"] == latest.completed_at.isoformat()


@pytest.mark.django_db
def test_list_only_users_workspaces_get_field(client, user, workspace, tenant_schema):
    # Sibling workspace this user does not belong to
    other_owner_email = "stranger@example.com"
    other = get_user_model().objects.create_user(email=other_owner_email, password="pass")
    other_ws = Workspace.objects.create(name="Other", created_by=other)
    WorkspaceMembership.objects.create(workspace=other_ws, user=other, role=WorkspaceRole.MANAGE)

    client.force_login(user)
    resp = client.get("/api/workspaces/")
    ids = [w["id"] for w in resp.json()]
    assert str(other_ws.id) not in ids


@pytest.mark.django_db
def test_detail_returns_null_when_no_completed_runs(client, user, workspace):
    client.force_login(user)
    resp = client.get(f"/api/workspaces/{workspace.id}/")
    assert resp.status_code == 200
    assert resp.json()["last_synced_at"] is None


@pytest.mark.django_db
def test_detail_returns_latest_completed_run(client, user, workspace, tenant_schema):
    now = timezone.now()
    older = now - timedelta(hours=2)
    _make_run(tenant_schema, MaterializationRun.RunState.COMPLETED, older)
    latest = _make_run(tenant_schema, MaterializationRun.RunState.COMPLETED, now)

    client.force_login(user)
    resp = client.get(f"/api/workspaces/{workspace.id}/")
    assert resp.json()["last_synced_at"] == latest.completed_at.isoformat()


@pytest.mark.django_db
def test_detail_ignores_in_flight_and_failed_runs(client, user, workspace, tenant_schema):
    now = timezone.now()
    completed = _make_run(
        tenant_schema, MaterializationRun.RunState.COMPLETED, now - timedelta(hours=1)
    )
    _make_run(tenant_schema, MaterializationRun.RunState.LOADING, now)
    _make_run(tenant_schema, MaterializationRun.RunState.FAILED, now)

    client.force_login(user)
    resp = client.get(f"/api/workspaces/{workspace.id}/")
    assert resp.json()["last_synced_at"] == completed.completed_at.isoformat()
