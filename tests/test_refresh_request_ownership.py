"""Refresh requests are bound to one candidate, actor, workspace, and queue job."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob
from rest_framework.test import APIClient

from apps.common.error_codes import ErrorCode
from apps.common.identifiers import tenant_schema_name
from apps.users.models import TenantMembership
from apps.workspaces.models import (
    SchemaState,
    TenantSchema,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    WorkspaceTenant,
)
from apps.workspaces.services.refresh_requests import (
    REFRESH_TASK_NAME,
    claim_refresh_candidate,
    find_legacy_refresh_jobs,
    reconcile_legacy_refresh_candidates,
    refresh_task_args,
)
from apps.workspaces.tasks import drop_failed_refresh_schema, refresh_tenant_schema
from config.procrastinate import JOB_RETENTION_HOURS


@pytest.fixture
def manage_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def refresh_job(db):
    created = []

    def make(args, *, status="doing", task_name=REFRESH_TASK_NAME):
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args) "
                "VALUES (%s, %s, %s::procrastinate_job_status, %s::jsonb) RETURNING id",
                ["default", task_name, status, json.dumps(args)],
            )
            job_id = cursor.fetchone()[0]
        created.append(job_id)
        return job_id

    yield make

    if created:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [created])


@pytest.fixture(autouse=True)
def queued_schema_drops():
    # Reconciliation queues real teardown jobs; procrastinate_jobs is unmanaged, so
    # a real defer would outlive the test.
    with patch("apps.workspaces.api.views.drop_failed_refresh_schema.defer") as drop:
        yield drop


def _bound_candidate(tenant, workspace, membership, refresh_job, *, state=SchemaState.PROVISIONING):
    candidate = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"owned_refresh_{TenantSchema.objects.count()}",
        state=state,
        refresh_workspace_id=workspace.id,
        refresh_actor_user_id=membership.user_id,
        refresh_membership_id=membership.id,
    )
    args = {
        "schema_id": str(candidate.id),
        "membership_id": str(membership.id),
        "actor_user_id": str(membership.user_id),
        "workspace_id": str(workspace.id),
    }
    job_id = refresh_job(args)
    candidate.refresh_job_id = job_id
    candidate.save(update_fields=["refresh_job_id"])
    return candidate, args, job_id


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", [SchemaState.ACTIVE, SchemaState.MATERIALIZING])
def test_duplicate_delivery_never_touches_serving_or_claimed_candidate(
    workspace, tenant, tenant_membership, refresh_job, state
):
    candidate, args, job_id = _bound_candidate(
        tenant, workspace, tenant_membership, refresh_job, state=state
    )

    with (
        patch("apps.workspaces.tasks.run_pipeline") as pipeline,
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
        patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create,
    ):
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
        )

    candidate.refresh_from_db()
    assert result["status"] == "ignored"
    assert candidate.state == state
    create.assert_not_called()
    pipeline.assert_not_called()
    teardown.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mismatch", ["job_id", "task_name", "args", "status", "workspace"])
def test_mismatched_refresh_binding_is_a_noop(
    workspace, tenant, tenant_membership, refresh_job, mismatch
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    context_job_id = job_id
    if mismatch == "job_id":
        context_job_id = refresh_job(args)
    elif mismatch == "task_name":
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE procrastinate_jobs SET task_name = %s WHERE id = %s",
                ["apps.workspaces.tasks.teardown_schema", job_id],
            )
    elif mismatch == "args":
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE procrastinate_jobs SET args = %s::jsonb WHERE id = %s",
                [json.dumps({**args, "workspace_id": str(tenant.id)}), job_id],
            )
    elif mismatch == "status":
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status WHERE id = %s",
                ["todo", job_id],
            )
    else:
        other = Workspace.objects.create(name="Other", created_by=tenant_membership.user)
        WorkspaceMembership.objects.create(
            workspace=other, user=tenant_membership.user, role=WorkspaceRole.MANAGE
        )
        args["workspace_id"] = str(other.id)

    with patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create:
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=context_job_id)), **args
        )

    candidate.refresh_from_db()
    assert result["status"] == "rejected"
    assert result["error_code"] == ErrorCode.REFRESH_REQUEST_MISMATCH
    assert "role" not in result["error"].lower()
    assert candidate.state == SchemaState.PROVISIONING
    create.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_owned_request_losing_role_settles_only_its_candidate(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    serving = TenantSchema.objects.create(
        tenant=tenant, schema_name="serving_refresh", state=SchemaState.ACTIVE
    )
    WorkspaceMembership.objects.filter(workspace=workspace, user=tenant_membership.user).update(
        role=WorkspaceRole.READ
    )

    with (
        patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create,
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
    ):
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
        )

    candidate.refresh_from_db()
    serving.refresh_from_db()
    assert result["status"] == "denied"
    assert result["error_code"] == ErrorCode.WORKSPACE_ROLE_INSUFFICIENT
    assert "role" in result["error"].lower()
    assert result["retry_required"] is True
    assert candidate.state == SchemaState.FAILED
    assert serving.state == SchemaState.ACTIVE
    create.assert_not_called()
    teardown.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_missing_membership_cannot_demote_active_candidate(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(
        tenant, workspace, tenant_membership, refresh_job, state=SchemaState.ACTIVE
    )
    TenantMembership.objects.filter(id=tenant_membership.id).delete()

    with (
        patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create,
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
    ):
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
        )

    candidate.refresh_from_db()
    assert result["status"] == "ignored"
    assert candidate.state == SchemaState.ACTIVE
    create.assert_not_called()
    teardown.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_pipeline_failure_cleanup_cannot_drop_candidate_that_became_active(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    pipeline = MagicMock(provider=tenant.provider, name="refresh_pipeline")
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline

    def activate_then_fail(*_args, **_kwargs):
        TenantSchema.objects.filter(id=candidate.id).update(state=SchemaState.ACTIVE)
        raise RuntimeError("late pipeline failure")

    with (
        patch("apps.workspaces.services.schema_manager.get_managed_db_connection"),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            return_value={"type": "api_key", "value": "token"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=registry),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=activate_then_fail),
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
    ):
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
        )

    candidate.refresh_from_db()
    assert result["error"] == "Materialization failed"
    assert candidate.state == SchemaState.ACTIVE
    teardown.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_concurrent_claim_has_one_winner(workspace, tenant, tenant_membership, refresh_job):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    barrier = threading.Barrier(2)

    def claim():
        try:
            barrier.wait(timeout=10)
            return claim_refresh_candidate(job_id=job_id, **args).status
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = {
            future.result(timeout=10) for future in [executor.submit(claim) for _ in range(2)]
        }

    candidate.refresh_from_db()
    assert outcomes == {"claimed", "ignored"}
    assert candidate.state == SchemaState.PROVISIONING
    assert candidate.refresh_claimed_at is not None


@pytest.mark.django_db(transaction=True)
def test_terminal_bound_claim_is_reconciled_and_retryable(
    manage_client, workspace, tenant, tenant_membership, refresh_job
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    candidate.refresh_claimed_at = timezone.now()
    candidate.save(update_fields=["refresh_claimed_at"])
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status WHERE id = %s",
            ["cancelled", job_id],
        )

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        defer.return_value = MagicMock(id=987655)
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    candidate.refresh_from_db()
    assert response.status_code == 202
    assert candidate.state == SchemaState.FAILED
    assert TenantSchema.objects.get(id=response.data["schema_id"]).refresh_job_id == 987655


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("job_status", ["doing", "aborting"])
def test_doing_bound_claim_remains_in_progress(
    manage_client, workspace, tenant, tenant_membership, refresh_job, job_status
):
    candidate, _args, _job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status WHERE id = %s",
            [job_status, _job_id],
        )
    candidate.refresh_claimed_at = timezone.now()
    candidate.save(update_fields=["refresh_claimed_at"])

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    candidate.refresh_from_db()
    assert response.status_code == 409
    assert response.data == {"error": "A refresh is already in progress."}
    assert candidate.state == SchemaState.PROVISIONING
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_malformed_bound_claim_requires_operator_recovery(
    manage_client, workspace, tenant, tenant_membership, refresh_job
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE procrastinate_jobs SET args = %s::jsonb WHERE id = %s",
            [json.dumps({"schema_id": str(candidate.id)}), job_id],
        )

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    candidate.refresh_from_db()
    assert response.status_code == 409
    assert response.data["code"] == "refresh_recovery_required"
    assert candidate.state == SchemaState.PROVISIONING
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_terminal_legacy_candidate_is_reconciled_and_retryable(
    manage_client, workspace, tenant, tenant_membership, refresh_job
):
    legacy = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_refresh", state=SchemaState.PROVISIONING
    )
    refresh_job(
        {"schema_id": str(legacy.id), "membership_id": str(tenant_membership.id)},
        status="failed",
    )

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        defer.return_value = MagicMock(id=987654)
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    legacy.refresh_from_db()
    assert response.status_code == 202
    assert legacy.state == SchemaState.FAILED
    assert TenantSchema.objects.get(id=response.data["schema_id"]).refresh_job_id == 987654


@pytest.mark.django_db(transaction=True)
def test_dispatch_and_candidate_binding_roll_back_together(
    manage_client, workspace, tenant, tenant_membership
):
    before_jobs = ProcrastinateJob.objects.filter(task_name=REFRESH_TASK_NAME).count()
    original_save = TenantSchema.save

    def fail_binding_save(schema, *args, **kwargs):
        if "refresh_job_id" in (kwargs.get("update_fields") or ()):
            raise RuntimeError("binding write failed")
        return original_save(schema, *args, **kwargs)

    with patch.object(TenantSchema, "save", fail_binding_save):
        with pytest.raises(RuntimeError, match="binding write failed"):
            manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    assert not TenantSchema.objects.filter(tenant=tenant, state=SchemaState.PROVISIONING).exists()
    assert ProcrastinateJob.objects.filter(task_name=REFRESH_TASK_NAME).count() == before_jobs


@pytest.mark.django_db(transaction=True)
def test_concurrent_refresh_posts_create_one_bound_job(workspace, tenant, tenant_membership):
    user_id = tenant_membership.user_id
    workspace_id = workspace.id
    barrier = threading.Barrier(2)

    def post_refresh():
        try:
            client = APIClient()
            user = tenant_membership.user.__class__.objects.get(id=user_id)
            client.force_authenticate(user=user)
            barrier.wait(timeout=10)
            return client.post(f"/api/workspaces/{workspace_id}/refresh/").status_code
        finally:
            connection.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(
                future.result(timeout=15)
                for future in [executor.submit(post_refresh) for _ in range(2)]
            )

        assert statuses == [202, 409]
        candidate = TenantSchema.objects.get(tenant=tenant, state=SchemaState.PROVISIONING)
        assert candidate.refresh_job_id is not None
        job = ProcrastinateJob.objects.get(id=candidate.refresh_job_id)
        assert job.task_name == refresh_tenant_schema.name == REFRESH_TASK_NAME
        assert job.args == refresh_task_args(candidate)
    finally:
        # Queue rows are unmanaged; remove this test's jobs even after an assertion fails.
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM procrastinate_jobs WHERE task_name = %s AND args->>'workspace_id' = %s",
                [REFRESH_TASK_NAME, str(workspace_id)],
            )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("status", "extra_args"),
    [("todo", {}), ("doing", {}), ("aborting", {}), ("failed", {"unexpected": "value"})],
)
def test_unverified_legacy_candidate_still_blocks_retry(
    manage_client,
    workspace,
    tenant,
    tenant_membership,
    refresh_job,
    status,
    extra_args,
):
    legacy = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"legacy_{status}_{bool(extra_args)}",
        state=SchemaState.PROVISIONING,
    )
    refresh_job(
        {
            "schema_id": str(legacy.id),
            "membership_id": str(tenant_membership.id),
            **extra_args,
        },
        status=status,
    )

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    legacy.refresh_from_db()
    assert response.status_code == 409
    assert legacy.state == SchemaState.PROVISIONING
    defer.assert_not_called()
    if status in {"todo", "doing", "aborting"}:
        assert response.data == {"error": "A refresh is already in progress."}
    else:
        assert response.data["code"] == "refresh_recovery_required"
        assert "operator" in response.data["error"].lower()


@pytest.mark.django_db(transaction=True)
def test_missing_or_duplicate_legacy_job_still_blocks_retry(
    manage_client, workspace, tenant, tenant_membership, refresh_job
):
    missing = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_missing_job", state=SchemaState.PROVISIONING
    )
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert response.status_code == 409
    assert response.data["code"] == "refresh_recovery_required"
    assert "operator" in response.data["error"].lower()
    missing.refresh_from_db()
    assert missing.state == SchemaState.PROVISIONING
    defer.assert_not_called()

    missing.delete()
    duplicate = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_duplicate_jobs", state=SchemaState.PROVISIONING
    )
    args = {"schema_id": str(duplicate.id), "membership_id": str(tenant_membership.id)}
    refresh_job(args, status="failed")
    refresh_job(args, status="failed")
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert response.status_code == 409
    assert response.data["code"] == "refresh_recovery_required"
    duplicate.refresh_from_db()
    assert duplicate.state == SchemaState.PROVISIONING
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_wrong_tenant_legacy_membership_still_blocks_retry(
    manage_client, workspace, tenant, tenant_membership, refresh_job, user
):
    other_tenant = tenant.__class__.objects.create(
        provider="commcare", external_id="other-refresh-tenant", canonical_name="Other"
    )
    other_membership = TenantMembership.objects.create(user=user, tenant=other_tenant)
    legacy = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_wrong_tenant", state=SchemaState.PROVISIONING
    )
    refresh_job(
        {"schema_id": str(legacy.id), "membership_id": str(other_membership.id)},
        status="failed",
    )

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    legacy.refresh_from_db()
    assert response.status_code == 409
    assert response.data["code"] == "refresh_recovery_required"
    assert "operator" in response.data["error"].lower()
    assert legacy.state == SchemaState.PROVISIONING
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_unexpected_membership_lookup_error_does_not_settle_candidate(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)

    with (
        patch(
            "apps.workspaces.services.refresh_requests.TenantMembership.objects.select_related"
        ) as select_related,
        pytest.raises(ValueError, match="driver bug"),
    ):
        select_related.return_value.get.side_effect = ValueError("driver bug")
        claim_refresh_candidate(job_id=job_id, **args)

    candidate.refresh_from_db()
    assert candidate.state == SchemaState.PROVISIONING
    assert candidate.refresh_claimed_at is None


@pytest.mark.django_db(transaction=True)
def test_owned_request_whose_membership_vanished_reports_lost_tenant_access(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    TenantMembership.objects.filter(id=tenant_membership.id).delete()

    with patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create:
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
        )

    candidate.refresh_from_db()
    assert result["status"] == "denied"
    assert result["error_code"] == ErrorCode.WORKSPACE_TENANT_UNREACHABLE
    assert "role" not in result["error"].lower()
    assert candidate.state == SchemaState.FAILED
    create.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_owned_request_for_unlinked_tenant_reports_unlinked_workspace(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).delete()

    with patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create:
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
        )

    candidate.refresh_from_db()
    assert result["status"] == "denied"
    assert result["error_code"] == ErrorCode.WORKSPACE_TENANT_UNREACHABLE
    assert "no longer part of" in result["error"]
    assert "role" not in result["error"].lower()
    assert candidate.state == SchemaState.FAILED
    create.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_initial_provision_row_is_not_treated_as_unverifiable_refresh(
    manage_client, workspace, tenant, tenant_membership
):
    # SchemaManager.provision() holds its base-named row in PROVISIONING while it
    # runs CREATE SCHEMA; no refresh job ever exists for it.
    base = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=tenant_schema_name(tenant.provider, tenant.external_id),
        state=SchemaState.PROVISIONING,
    )

    legacy_jobs = find_legacy_refresh_jobs(tenant)
    assert reconcile_legacy_refresh_candidates(tenant, legacy_jobs).recovery_needed is False
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    base.refresh_from_db()
    assert response.status_code == 409
    assert response.data == {"error": "A refresh is already in progress."}
    assert base.state == SchemaState.PROVISIONING
    defer.assert_not_called()


def _age_past_job_retention(candidate):
    TenantSchema.objects.filter(id=candidate.id).update(
        created_at=timezone.now() - timedelta(hours=JOB_RETENTION_HOURS + 1)
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("binding", ["bound", "legacy"])
def test_candidate_whose_job_was_pruned_is_reconciled_and_retryable(
    manage_client, workspace, tenant, tenant_membership, refresh_job, binding
):
    # prune_old_procrastinate_jobs deletes succeeded jobs after JOB_RETENTION_HOURS;
    # a denied or mismatched delivery finishes "succeeded" and leaves its candidate
    # PROVISIONING, so the queue evidence can legitimately disappear.
    if binding == "bound":
        candidate, _args, job_id = _bound_candidate(
            tenant, workspace, tenant_membership, refresh_job
        )
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [job_id])
    else:
        candidate = TenantSchema.objects.create(
            tenant=tenant, schema_name="legacy_pruned_job", state=SchemaState.PROVISIONING
        )
    _age_past_job_retention(candidate)

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        defer.return_value = MagicMock(id=987656)
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    candidate.refresh_from_db()
    assert response.status_code == 202
    assert candidate.state == SchemaState.FAILED
    defer.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_bound_candidate_missing_its_job_within_retention_still_blocks_retry(
    manage_client, workspace, tenant, tenant_membership, refresh_job
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [job_id])

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    candidate.refresh_from_db()
    assert response.status_code == 409
    assert response.data["code"] == "refresh_recovery_required"
    assert candidate.state == SchemaState.PROVISIONING
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_reconciled_candidate_has_its_physical_schema_dropped(
    manage_client, workspace, tenant, tenant_membership, refresh_job, queued_schema_drops
):
    # The worker may have died after CREATE SCHEMA or mid-load; nothing else ever
    # sweeps FAILED rows, so settling one must also queue its physical drop.
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status WHERE id = %s",
            ["failed", job_id],
        )

    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        defer.return_value = MagicMock(id=987657)
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    assert response.status_code == 202
    queued_schema_drops.assert_called_once_with(schema_id=str(candidate.id))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("state", "dropped"),
    [(SchemaState.FAILED, True), (SchemaState.ACTIVE, False), (SchemaState.PROVISIONING, False)],
)
def test_failed_refresh_drop_only_touches_failed_rows(tenant, state, dropped):
    schema = TenantSchema.objects.create(tenant=tenant, schema_name="dropped_r1", state=state)

    with patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown:
        async_to_sync(drop_failed_refresh_schema.func)(schema_id=str(schema.id))

    assert teardown.called is dropped
    schema.refresh_from_db()
    assert schema.state == state


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("taken_to", "dropped"), [(SchemaState.FAILED, True), (SchemaState.ACTIVE, False)]
)
def test_lost_activation_drops_the_loaded_schema_only_if_it_was_failed(
    workspace, tenant, tenant_membership, refresh_job, taken_to, dropped
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    pipeline = MagicMock(provider=tenant.provider, name="refresh_pipeline")
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline

    def load_then_lose_ownership(*_args, **_kwargs):
        TenantSchema.objects.filter(id=candidate.id).update(state=taken_to)

    with (
        patch("apps.workspaces.services.schema_manager.get_managed_db_connection"),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            return_value={"type": "api_key", "value": "token"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=registry),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=load_then_lose_ownership),
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
    ):
        result = async_to_sync(refresh_tenant_schema.func)(
            context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
        )

    candidate.refresh_from_db()
    assert result == {"status": "ignored"}
    assert candidate.state == taken_to
    assert [call.args[0].id for call in teardown.call_args_list] == (
        [candidate.id] if dropped else []
    )


@pytest.mark.django_db(transaction=True)
def test_legacy_job_search_runs_before_the_tenant_lock(
    manage_client, workspace, tenant, tenant_membership, refresh_job
):
    # procrastinate_jobs.args has no index for a schema_id lookup; scanning it while
    # holding the tenant lock would serialize every refresh behind a table scan.
    legacy = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_lock_order", state=SchemaState.PROVISIONING
    )
    refresh_job(
        {"schema_id": str(legacy.id), "membership_id": str(tenant_membership.id)},
        status="failed",
    )

    with (
        CaptureQueriesContext(connection) as queries,
        patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer,
    ):
        defer.return_value = MagicMock(id=987658)
        response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")

    assert response.status_code == 202
    sql = [query["sql"] for query in queries.captured_queries]
    searches = [i for i, q in enumerate(sql) if "procrastinate_jobs" in q and "'schema_id'" in q]
    tenant_lock = next(
        i for i, q in enumerate(sql) if 'FROM "users_tenant"' in q and "FOR UPDATE" in q
    )
    assert searches
    assert max(searches) < tenant_lock
    assert all("FOR UPDATE" not in sql[i] for i in searches)
