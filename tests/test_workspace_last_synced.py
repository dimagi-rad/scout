"""Tests for the last_synced_at field on workspace endpoints."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.users.models import Tenant, TenantMembership
from apps.workspaces.api.workspace_views import _derive_schema_status
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


# ── Live schema_status on the LIST endpoint ──────────────────────────────────


def _list_entry(client, user, workspace):
    client.force_login(user)
    resp = client.get("/api/workspaces/")
    assert resp.status_code == 200
    return next(w for w in resp.json() if w["id"] == str(workspace.id))


@pytest.mark.django_db
def test_list_schema_status_unavailable_without_schema(client, user, workspace):
    # No TenantSchema at all → live state is unavailable.
    assert _list_entry(client, user, workspace)["schema_status"] == "unavailable"


@pytest.mark.django_db
def test_list_schema_status_available_when_active(client, user, workspace, tenant_schema):
    # tenant_schema fixture is ACTIVE for the workspace's single tenant.
    assert _list_entry(client, user, workspace)["schema_status"] == "available"


@pytest.mark.django_db
def test_list_schema_status_provisioning(client, user, workspace, tenant):
    TenantSchema.objects.create(
        tenant=tenant, schema_name="prov_schema", state=SchemaState.PROVISIONING
    )
    assert _list_entry(client, user, workspace)["schema_status"] == "provisioning"


@pytest.mark.django_db
def test_list_schema_status_matches_detail(client, user, workspace, tenant_schema):
    """List and detail endpoints must agree on schema_status."""
    list_status = _list_entry(client, user, workspace)["schema_status"]
    detail = client.get(f"/api/workspaces/{workspace.id}/").json()
    assert list_status == detail["schema_status"] == "available"


# ── _derive_schema_status: multi-tenant view-schema states ───────────────────


def test_derive_schema_status_multi_tenant_failed_view_schema():
    """A FAILED multi-tenant view schema yields the distinct 'failed' status,
    not the generic 'provisioning' bucket — so the UI/agent can surface it."""
    assert (
        _derive_schema_status(
            tenant_count=2,
            active_count=2,
            provisioning=False,
            view_schema_state=SchemaState.FAILED,
        )
        == "failed"
    )


def test_derive_schema_status_multi_tenant_active_view_schema():
    assert (
        _derive_schema_status(
            tenant_count=2,
            active_count=2,
            provisioning=False,
            view_schema_state=SchemaState.ACTIVE,
        )
        == "available"
    )


def test_derive_schema_status_multi_tenant_missing_view_schema_is_provisioning():
    assert (
        _derive_schema_status(
            tenant_count=2,
            active_count=2,
            provisioning=False,
            view_schema_state=None,
        )
        == "provisioning"
    )


# ── PARTIAL runs are data-bearing ────────────────────────────────────────────


@pytest.mark.django_db
def test_list_counts_partial_runs(client, user, workspace, tenant_schema):
    """A PARTIAL run loaded some sources — that data is queryable, so it synced."""
    partial = _make_run(tenant_schema, MaterializationRun.RunState.PARTIAL, timezone.now())

    entry = _list_entry(client, user, workspace)
    assert entry["last_synced_at"] == partial.completed_at.isoformat()


@pytest.mark.django_db
def test_detail_counts_partial_runs(client, user, workspace, tenant_schema):
    partial = _make_run(tenant_schema, MaterializationRun.RunState.PARTIAL, timezone.now())

    client.force_login(user)
    resp = client.get(f"/api/workspaces/{workspace.id}/")
    assert resp.json()["last_synced_at"] == partial.completed_at.isoformat()


@pytest.mark.django_db
def test_partial_run_does_not_contradict_schema_status(client, user, workspace, tenant_schema):
    """An 'available' schema must never be reported as never-synced (issue #410).

    Reproduced on main @ 22889a7: a PARTIAL run with completed_at set returned
    schema_status='available' and last_synced_at=None in the same payload.
    """
    _make_run(tenant_schema, MaterializationRun.RunState.PARTIAL, timezone.now())

    entry = _list_entry(client, user, workspace)
    detail = client.get(f"/api/workspaces/{workspace.id}/").json()

    assert entry["schema_status"] == detail["schema_status"] == "available"
    assert entry["last_synced_at"] is not None
    assert entry["last_synced_at"] == detail["last_synced_at"]


@pytest.mark.django_db
def test_list_prefers_latest_across_completed_and_partial(client, user, workspace, tenant_schema):
    now = timezone.now()
    _make_run(tenant_schema, MaterializationRun.RunState.COMPLETED, now - timedelta(hours=2))
    latest = _make_run(tenant_schema, MaterializationRun.RunState.PARTIAL, now)

    assert _list_entry(client, user, workspace)["last_synced_at"] == latest.completed_at.isoformat()


@pytest.mark.django_db
def test_stale_runs_still_ignored(client, user, workspace, tenant_schema):
    """Teardown flips data-bearing runs to STALE; the data is gone, so no sync."""
    _make_run(tenant_schema, MaterializationRun.RunState.STALE, timezone.now())

    assert _list_entry(client, user, workspace)["last_synced_at"] is None


@pytest.mark.django_db
def test_run_without_completed_at_does_not_shadow_real_sync(client, user, workspace, tenant_schema):
    """Postgres sorts NULLs first under DESC, so an untimestamped run must be
    filtered out rather than winning the ordering and reporting null."""
    completed = _make_run(tenant_schema, MaterializationRun.RunState.COMPLETED, timezone.now())
    _make_run(tenant_schema, MaterializationRun.RunState.PARTIAL, None)

    entry = _list_entry(client, user, workspace)
    detail = client.get(f"/api/workspaces/{workspace.id}/").json()
    assert entry["last_synced_at"] == completed.completed_at.isoformat()
    assert detail["last_synced_at"] == completed.completed_at.isoformat()


# ── Query-count guarantee on the list endpoint ───────────────────────────────


def _extra_workspaces(user, count, *, offset):
    """Give *user* ``count`` more workspaces, each with its own tenant, ACTIVE
    schema and a data-bearing run."""
    for i in range(offset, offset + count):
        t = Tenant.objects.create(
            provider="commcare", external_id=f"bulk-{i}", canonical_name=f"Bulk {i}"
        )
        ws = Workspace.objects.create(name=t.canonical_name, created_by=user)
        WorkspaceTenant.objects.create(workspace=ws, tenant=t)
        WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceRole.MANAGE)
        TenantMembership.objects.bulk_create(
            [TenantMembership(user=user, tenant=t)], ignore_conflicts=True
        )
        schema = TenantSchema.objects.create(
            tenant=t, schema_name=f"bulk_schema_{i}", state=SchemaState.ACTIVE
        )
        _make_run(schema, MaterializationRun.RunState.PARTIAL, timezone.now())


@pytest.mark.django_db
def test_list_query_count_does_not_scale_with_workspaces(client, user, workspace, tenant_schema):
    """last_synced_at and schema_status must stay bulk-derived.

    The list endpoint costs a fixed set of queries plus exactly one per
    workspace, from the ``Workspace.display_name`` -> ``tenant`` property (which
    the prefetch does not cover). last_synced_at is a Subquery annotation and
    schema_status comes from ``_schema_status_for_workspaces``; deriving either
    per workspace pushes the slope to ~5, i.e. ~4,000 queries for the 800+
    workspaces in issue #410, against a shared RDS that has already hit
    connection exhaustion.
    """
    client.force_login(user)

    small_n, large_n = 4, 16
    _extra_workspaces(user, small_n - 1, offset=0)
    with CaptureQueriesContext(connection) as small:
        assert len(client.get("/api/workspaces/").json()) == small_n

    _extra_workspaces(user, large_n - small_n, offset=100)
    with CaptureQueriesContext(connection) as large:
        assert len(client.get("/api/workspaces/").json()) == large_n

    per_workspace = (len(large) - len(small)) / (large_n - small_n)
    assert per_workspace <= 1, (
        f"list endpoint costs {per_workspace} queries per workspace "
        f"({len(small)} for {small_n}, {len(large)} for {large_n}) — "
        "something per-workspace was added where a bulk query is required"
    )
