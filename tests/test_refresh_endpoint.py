"""Tests for data refresh endpoint (Task 4.1)."""

from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from rest_framework.test import APIClient

from apps.users.models import Tenant
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.tasks import refresh_tenant_schema
from tests.tenant_access import grant_tenant_access


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def manage_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def tenant_membership_for_user(db, user, tenant):
    from apps.users.models import TenantMembership

    # Use get_or_create since the workspace signal may have already created one
    tm, _ = TenantMembership.objects.get_or_create(user=user, tenant=tenant)
    return tm


@pytest.mark.django_db
def test_refresh_returns_202(manage_client, workspace, tenant_membership_for_user):
    with patch(
        "apps.workspaces.api.views.refresh_tenant_schema.defer",
        return_value=MagicMock(id=438),
    ):
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 202
    assert resp.data["status"] == "provisioning"
    assert "schema_id" in resp.data


@pytest.mark.django_db
def test_refresh_creates_provisioning_schema(
    manage_client, workspace, tenant, tenant_membership_for_user
):
    with patch(
        "apps.workspaces.api.views.refresh_tenant_schema.defer",
        return_value=MagicMock(id=438),
    ):
        manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert TenantSchema.objects.filter(tenant=tenant, state=SchemaState.PROVISIONING).exists()


@pytest.mark.django_db
def test_refresh_dispatches_task(manage_client, workspace, tenant_membership_for_user):
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as mock_defer:
        mock_defer.return_value = MagicMock(id=438)
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    schema_id = resp.data["schema_id"]
    mock_defer.assert_called_once_with(
        schema_id=schema_id,
        membership_id=str(tenant_membership_for_user.id),
        actor_user_id=str(tenant_membership_for_user.user_id),
        workspace_id=str(workspace.id),
    )


@pytest.mark.django_db
def test_read_only_user_cannot_trigger_refresh(api_client, read_user, workspace):
    api_client.force_authenticate(user=read_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_queued_refresh_downgrade_releases_provisioning_guard(
    manage_client, workspace, tenant_membership_for_user
):
    job_ids = []
    original_defer = refresh_tenant_schema.defer

    def defer_and_record(**kwargs):
        job = original_defer(**kwargs)
        job_ids.append(getattr(job, "id", job))
        return job

    try:
        with patch(
            "apps.workspaces.api.views.refresh_tenant_schema.defer", side_effect=defer_and_record
        ) as defer:
            accepted = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
        assert accepted.status_code == 202
        queued = defer.call_args.kwargs
        job_id = job_ids[0]
        with connection.cursor() as cursor:
            cursor.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", [job_id])
        WorkspaceMembership.objects.filter(
            workspace=workspace, user=tenant_membership_for_user.user
        ).update(role=WorkspaceRole.READ)
        with patch("apps.workspaces.tasks.run_pipeline") as pipeline:
            result = async_to_sync(refresh_tenant_schema.func)(
                context=MagicMock(job=MagicMock(id=job_id)), **queued
            )
        assert result["status"] == "denied"
        assert result["retry_required"] is True
        assert TenantSchema.objects.get(id=queued["schema_id"]).state == SchemaState.FAILED
        pipeline.assert_not_called()
        WorkspaceMembership.objects.filter(
            workspace=workspace, user=tenant_membership_for_user.user
        ).update(role=WorkspaceRole.MANAGE)
        with patch(
            "apps.workspaces.api.views.refresh_tenant_schema.defer", side_effect=defer_and_record
        ):
            retry = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
        assert retry.status_code == 202
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [job_ids])


@pytest.mark.django_db
def test_non_member_cannot_trigger_refresh(api_client, other_user, workspace):
    api_client.force_authenticate(user=other_user)
    resp = api_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_refresh_status_returns_schema_state(manage_client, workspace, tenant):
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name="status_test_schema",
        state=SchemaState.ACTIVE,
    )
    resp = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")
    assert resp.status_code == 200
    assert resp.data["state"] == SchemaState.ACTIVE


@pytest.mark.django_db
def test_refresh_status_returns_materializing_state(manage_client, workspace, tenant):
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name="status_materializing_schema",
        state=SchemaState.MATERIALIZING,
    )
    resp = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")
    assert resp.status_code == 200
    assert resp.data["state"] == SchemaState.MATERIALIZING


@pytest.mark.django_db
def test_refresh_status_no_schema_returns_unavailable(manage_client, workspace):
    resp = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")
    assert resp.status_code == 200
    assert resp.data["state"] == "unavailable"


def _add_source(workspace, user, external_id):
    extra = Tenant.objects.create(
        provider="commcare", external_id=external_id, canonical_name=external_id
    )
    WorkspaceTenant.objects.create(workspace=workspace, tenant=extra)
    grant_tenant_access(user, extra)
    return extra


@pytest.mark.django_db
def test_refresh_covers_every_source_of_a_multi_source_workspace(
    manage_client, workspace, tenant, user, tenant_membership_for_user
):
    """Manual refresh used to refresh only the workspace's first tenant."""
    second = _add_source(workspace, user, "second-source")

    job_ids = iter([501, 502])
    with patch(
        "apps.workspaces.api.views.refresh_tenant_schema.defer",
        side_effect=lambda **_: MagicMock(id=next(job_ids)),
    ) as defer:
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    assert resp.status_code == 202
    assert defer.call_count == 2
    assert {t["tenant_id"] for t in resp.data["tenants"]} == {str(tenant.id), str(second.id)}
    assert all(t["status"] == "provisioning" for t in resp.data["tenants"])
    for source in (tenant, second):
        assert TenantSchema.objects.filter(tenant=source, state=SchemaState.PROVISIONING).exists()


@pytest.mark.django_db
def test_refresh_reports_each_source_that_could_not_start(
    manage_client, workspace, tenant, user, tenant_membership_for_user
):
    busy = _add_source(workspace, user, "busy-source")
    # A workspace load is filling a candidate for this source under its tenant lock.
    TenantSchema.objects.create(
        tenant=busy,
        schema_name="busy_r1",
        state=SchemaState.PROVISIONING,
        load_workspace_id=workspace.id,
    )

    with patch(
        "apps.workspaces.api.views.refresh_tenant_schema.defer", return_value=MagicMock(id=610)
    ) as defer:
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    assert resp.status_code == 202
    defer.assert_called_once()
    by_tenant = {t["tenant_id"]: t for t in resp.data["tenants"]}
    assert by_tenant[str(tenant.id)]["status"] == "provisioning"
    assert by_tenant[str(busy.id)]["status"] == "in_progress"
    # Each source carries its own schema id; none is promoted to the top level.
    assert "schema_id" not in resp.data
    assert by_tenant[str(tenant.id)]["schema_id"]
    assert not any({"body", "http_status"} & set(t) for t in resp.data["tenants"])


@pytest.mark.django_db
def test_refresh_status_reports_each_source_and_never_hides_a_failure(
    manage_client, workspace, tenant, user
):
    failed = _add_source(workspace, user, "failed-source")
    TenantSchema.objects.create(tenant=tenant, schema_name="ok_live", state=SchemaState.ACTIVE)
    TenantSchema.objects.create(tenant=failed, schema_name="bad_r1", state=SchemaState.FAILED)

    resp = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")

    assert resp.data["state"] == SchemaState.FAILED
    assert {t["tenant_id"]: t["state"] for t in resp.data["tenants"]} == {
        str(tenant.id): SchemaState.ACTIVE,
        str(failed.id): SchemaState.FAILED,
    }


@pytest.mark.django_db
def test_a_refresh_where_no_source_could_start_is_a_conflict(
    manage_client, workspace, tenant, user, tenant_membership_for_user
):
    second = _add_source(workspace, user, "second-busy")
    for source in (tenant, second):
        TenantSchema.objects.create(
            tenant=source,
            schema_name=f"busy_{source.external_id}",
            state=SchemaState.PROVISIONING,
            load_workspace_id=workspace.id,
        )

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        resp = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    assert resp.status_code == 409
    defer.assert_not_called()
    assert resp.data["status"] == "not_started"
    assert {t["status"] for t in resp.data["tenants"]} == {"in_progress"}
    assert "code" not in resp.data


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("states", "aggregate"),
    [
        ((SchemaState.PROVISIONING, SchemaState.FAILED), SchemaState.FAILED),
        ((SchemaState.ACTIVE, SchemaState.EXPIRED), SchemaState.EXPIRED),
        ((SchemaState.EXPIRED, SchemaState.ACTIVE), SchemaState.EXPIRED),
    ],
)
def test_refresh_status_aggregate_is_deterministic_and_matches_its_error(
    manage_client, workspace, tenant, user, states, aggregate
):
    second = _add_source(workspace, user, "second-status")
    for source, state in zip((tenant, second), states, strict=True):
        TenantSchema.objects.create(
            tenant=source, schema_name=f"s_{source.external_id}", state=state
        )

    resp = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")

    assert resp.data["state"] == aggregate
    assert (resp.data["error"] is not None) == (aggregate == SchemaState.FAILED)
