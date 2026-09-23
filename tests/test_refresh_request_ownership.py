"""Refresh requests are bound to one candidate, actor, workspace, and queue job."""

import json
import logging
import threading
import uuid
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
    DENIED_MEMBERSHIP_MISSING,
    DENIED_ROLE_REQUIRED,
    DENIED_WORKSPACE_UNLINKED,
    REFRESH_TASK_NAME,
    UNSCANNED_GRACE,
    RefreshClaim,
    activate_claimed_refresh_candidate,
    claim_refresh_candidate,
    fail_claimed_refresh_candidate,
    find_legacy_refresh_jobs,
    reconcile_legacy_refresh_candidates,
    refresh_task_args,
)
from apps.workspaces.tasks import (
    JOB_RETENTION_HOURS,
    MATERIALIZATION_STALLED_HEARTBEAT_SECONDS,
    drop_failed_refresh_schema,
    reconcile_refresh_candidates,
    refresh_tenant_schema,
)

JOB_RETENTION = timedelta(hours=JOB_RETENTION_HOURS)
STALLED_AFTER = timedelta(seconds=MATERIALIZATION_STALLED_HEARTBEAT_SECONDS)


@pytest.fixture
def queue_worker(db):
    created = []

    def make(*, heartbeat_age):
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO procrastinate_workers (last_heartbeat) VALUES (%s) RETURNING id",
                [timezone.now() - heartbeat_age],
            )
            worker_id = cursor.fetchone()[0]
        created.append(worker_id)
        return worker_id

    yield make

    if created:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE procrastinate_jobs SET worker_id = NULL WHERE worker_id = ANY(%s)",
                [created],
            )
            cursor.execute("DELETE FROM procrastinate_workers WHERE id = ANY(%s)", [created])


@pytest.fixture
def refresh_job(queue_worker):
    created = []
    live_worker = []

    def make(args, *, status="doing", task_name=REFRESH_TASK_NAME):
        # A started job always belongs to a worker; without one the reconciler
        # correctly reads it as stalled.
        if not live_worker:
            live_worker.append(queue_worker(heartbeat_age=timedelta(0)))
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, status, args, worker_id) "
                "VALUES (%s, %s, %s::procrastinate_job_status, %s::jsonb, %s) RETURNING id",
                ["default", task_name, status, json.dumps(args), live_worker[0]],
            )
            job_id = cursor.fetchone()[0]
        created.append(job_id)
        return job_id

    yield make

    if created:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM procrastinate_jobs WHERE id = ANY(%s)", [created])


def _set_job(job_id, *, status=None, args=None, task_name=None):
    with connection.cursor() as cursor:
        if status is not None:
            cursor.execute(
                "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status WHERE id = %s",
                [status, job_id],
            )
        if args is not None:
            cursor.execute(
                "UPDATE procrastinate_jobs SET args = %s::jsonb WHERE id = %s",
                [json.dumps(args), job_id],
            )
        if task_name is not None:
            cursor.execute(
                "UPDATE procrastinate_jobs SET task_name = %s WHERE id = %s", [task_name, job_id]
            )


def _delete_job(job_id):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs WHERE id = %s", [job_id])


def _bound_candidate(tenant, workspace, membership, refresh_job, *, state=SchemaState.PROVISIONING):
    candidate = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"owned_refresh_{uuid.uuid4().hex[:12]}",
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


def _reconcile(tenant, legacy_jobs=None):
    now = timezone.now()
    return reconcile_legacy_refresh_candidates(
        tenant,
        legacy_jobs if legacy_jobs is not None else find_legacy_refresh_jobs(tenant),
        pruned_before=now - JOB_RETENTION,
        stalled_before=now - STALLED_AFTER,
    )


def _run_on_worker(job_id, worker_id, *, status="doing"):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE procrastinate_jobs SET status = %s::procrastinate_job_status, worker_id = %s "
            "WHERE id = %s",
            [status, worker_id, job_id],
        )


def _age_past_retention(candidate):
    TenantSchema.objects.filter(id=candidate.id).update(
        created_at=timezone.now() - JOB_RETENTION - timedelta(hours=1)
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", [SchemaState.ACTIVE, SchemaState.MATERIALIZING])
def test_duplicate_delivery_is_ignored_for_serving_candidate(
    workspace, tenant, tenant_membership, refresh_job, state
):
    candidate, args, job_id = _bound_candidate(
        tenant, workspace, tenant_membership, refresh_job, state=state
    )

    claim = claim_refresh_candidate(job_id=job_id, **args)

    candidate.refresh_from_db()
    assert claim.status == "ignored"
    assert candidate.state == state


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mismatch", ["job_id", "task_name", "args", "status", "workspace"])
def test_mismatched_binding_is_rejected_without_touching_candidate(
    workspace, tenant, tenant_membership, refresh_job, mismatch
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    claimed_job_id = job_id
    if mismatch == "job_id":
        claimed_job_id = refresh_job(args)
    elif mismatch == "task_name":
        _set_job(job_id, task_name="apps.workspaces.tasks.teardown_schema")
    elif mismatch == "args":
        _set_job(job_id, args={**args, "workspace_id": str(tenant.id)})
    elif mismatch == "status":
        _set_job(job_id, status="todo")
    else:
        other = Workspace.objects.create(name="Other", created_by=tenant_membership.user)
        WorkspaceMembership.objects.create(
            workspace=other, user=tenant_membership.user, role=WorkspaceRole.MANAGE
        )
        args["workspace_id"] = str(other.id)

    claim = claim_refresh_candidate(job_id=claimed_job_id, **args)

    candidate.refresh_from_db()
    assert claim.status == "rejected"
    assert candidate.state == SchemaState.PROVISIONING
    assert candidate.refresh_claimed_at is None


@pytest.mark.django_db(transaction=True)
def test_unknown_candidate_is_rejected(workspace, tenant_membership):
    claim = claim_refresh_candidate(
        schema_id="00000000-0000-0000-0000-000000000000",
        membership_id=str(tenant_membership.id),
        actor_user_id=str(tenant_membership.user_id),
        workspace_id=str(workspace.id),
        job_id=1,
    )

    assert claim.status == "rejected"


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

    claim = claim_refresh_candidate(job_id=job_id, **args)

    candidate.refresh_from_db()
    serving.refresh_from_db()
    assert (claim.status, claim.reason) == ("denied", DENIED_ROLE_REQUIRED)
    assert candidate.state == SchemaState.FAILED
    assert serving.state == SchemaState.ACTIVE


@pytest.mark.django_db(transaction=True)
def test_owned_request_whose_membership_vanished_is_denied_as_lost_access(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    TenantMembership.objects.filter(id=tenant_membership.id).delete()

    claim = claim_refresh_candidate(job_id=job_id, **args)

    candidate.refresh_from_db()
    assert (claim.status, claim.reason) == ("denied", DENIED_MEMBERSHIP_MISSING)
    assert candidate.state == SchemaState.FAILED


@pytest.mark.django_db(transaction=True)
def test_owned_request_for_unlinked_tenant_is_denied_as_unlinked(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).delete()

    claim = claim_refresh_candidate(job_id=job_id, **args)

    candidate.refresh_from_db()
    assert (claim.status, claim.reason) == ("denied", DENIED_WORKSPACE_UNLINKED)
    assert candidate.state == SchemaState.FAILED


@pytest.mark.django_db(transaction=True)
def test_missing_membership_cannot_demote_active_candidate(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(
        tenant, workspace, tenant_membership, refresh_job, state=SchemaState.ACTIVE
    )
    TenantMembership.objects.filter(id=tenant_membership.id).delete()

    claim = claim_refresh_candidate(job_id=job_id, **args)

    candidate.refresh_from_db()
    assert claim.status == "ignored"
    assert candidate.state == SchemaState.ACTIVE


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
def test_only_the_owning_job_can_activate_or_fail_a_claimed_candidate(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    assert claim_refresh_candidate(job_id=job_id, **args).status == "claimed"

    assert activate_claimed_refresh_candidate(candidate.id, job_id + 1, timezone.now()) is False
    assert fail_claimed_refresh_candidate(candidate.id, job_id + 1) is None
    assert activate_claimed_refresh_candidate(candidate.id, job_id, timezone.now()) is True
    assert fail_claimed_refresh_candidate(candidate.id, job_id) is None

    candidate.refresh_from_db()
    assert candidate.state == SchemaState.ACTIVE


@pytest.mark.django_db(transaction=True)
def test_owning_job_can_fail_its_claimed_candidate(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    assert claim_refresh_candidate(job_id=job_id, **args).status == "claimed"

    failed = fail_claimed_refresh_candidate(candidate.id, job_id)

    candidate.refresh_from_db()
    assert failed is not None
    assert failed.id == candidate.id
    assert candidate.state == SchemaState.FAILED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("job_status", ["aborted", "cancelled", "failed", "succeeded"])
def test_terminal_bound_candidate_is_settled(
    workspace, tenant, tenant_membership, refresh_job, job_status
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _set_job(job_id, status=job_status)

    result = _reconcile(tenant)

    candidate.refresh_from_db()
    assert result.recovery_needed is False
    assert result.settled_schema_ids == (candidate.id,)
    assert candidate.state == SchemaState.FAILED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("job_status", ["todo", "doing", "aborting"])
def test_in_flight_bound_candidate_is_left_running(
    workspace, tenant, tenant_membership, refresh_job, job_status
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _set_job(job_id, status=job_status)

    result = _reconcile(tenant)

    candidate.refresh_from_db()
    assert (result.recovery_needed, result.settled_schema_ids) == (False, ())
    assert candidate.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_malformed_bound_candidate_requires_operator_recovery(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _set_job(job_id, args={"schema_id": str(candidate.id)})

    result = _reconcile(tenant)

    candidate.refresh_from_db()
    assert result.recovery_needed is True
    assert candidate.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_terminal_legacy_candidate_is_settled(tenant, tenant_membership, refresh_job):
    legacy = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_refresh", state=SchemaState.PROVISIONING
    )
    refresh_job(
        {"schema_id": str(legacy.id), "membership_id": str(tenant_membership.id)},
        status="failed",
    )

    result = _reconcile(tenant)

    legacy.refresh_from_db()
    assert result.settled_schema_ids == (legacy.id,)
    assert legacy.state == SchemaState.FAILED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("status", "extra_args", "recovery_needed"),
    [
        ("todo", {}, False),
        ("doing", {}, False),
        ("aborting", {}, False),
        ("failed", {"unexpected": "value"}, True),
    ],
)
def test_unverified_legacy_candidate_is_never_settled(
    tenant, tenant_membership, refresh_job, status, extra_args, recovery_needed
):
    legacy = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=f"legacy_{status}_{bool(extra_args)}",
        state=SchemaState.PROVISIONING,
    )
    refresh_job(
        {"schema_id": str(legacy.id), "membership_id": str(tenant_membership.id), **extra_args},
        status=status,
    )

    result = _reconcile(tenant)

    legacy.refresh_from_db()
    assert result.recovery_needed is recovery_needed
    assert result.settled_schema_ids == ()
    assert legacy.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_missing_or_duplicate_legacy_job_requires_operator_recovery(
    tenant, tenant_membership, refresh_job
):
    missing = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_missing_job", state=SchemaState.PROVISIONING
    )
    assert _reconcile(tenant).recovery_needed is True
    missing.refresh_from_db()
    assert missing.state == SchemaState.PROVISIONING

    missing.delete()
    duplicate = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_duplicate_jobs", state=SchemaState.PROVISIONING
    )
    args = {"schema_id": str(duplicate.id), "membership_id": str(tenant_membership.id)}
    refresh_job(args, status="failed")
    refresh_job(args, status="failed")
    assert _reconcile(tenant).recovery_needed is True
    duplicate.refresh_from_db()
    assert duplicate.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_wrong_tenant_legacy_membership_requires_operator_recovery(tenant, refresh_job, user):
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

    result = _reconcile(tenant)

    legacy.refresh_from_db()
    assert result.recovery_needed is True
    assert legacy.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_initial_provision_row_is_not_treated_as_unverifiable_refresh(tenant):
    # SchemaManager.provision() holds its base-named row in PROVISIONING while it
    # runs CREATE SCHEMA; no refresh job ever exists for it.
    base = TenantSchema.objects.create(
        tenant=tenant,
        schema_name=tenant_schema_name(tenant.provider, tenant.external_id),
        state=SchemaState.PROVISIONING,
    )

    result = _reconcile(tenant)

    base.refresh_from_db()
    assert (result.recovery_needed, result.settled_schema_ids) == (False, ())
    assert base.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("binding", ["bound", "legacy"])
def test_candidate_whose_job_was_pruned_is_settled(
    workspace, tenant, tenant_membership, refresh_job, binding
):
    # prune_old_procrastinate_jobs deletes succeeded jobs after JOB_RETENTION_HOURS;
    # a rejected delivery finishes "succeeded" and leaves its candidate
    # PROVISIONING, so the queue evidence can legitimately disappear.
    if binding == "bound":
        candidate, _args, job_id = _bound_candidate(
            tenant, workspace, tenant_membership, refresh_job
        )
        _delete_job(job_id)
    else:
        candidate = TenantSchema.objects.create(
            tenant=tenant, schema_name="legacy_pruned_job", state=SchemaState.PROVISIONING
        )
    _age_past_retention(candidate)

    result = _reconcile(tenant)

    candidate.refresh_from_db()
    assert (result.recovery_needed, result.settled_schema_ids) == (False, (candidate.id,))
    assert candidate.state == SchemaState.FAILED


@pytest.mark.django_db(transaction=True)
def test_bound_candidate_missing_its_job_within_retention_requires_recovery(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _delete_job(job_id)

    result = _reconcile(tenant)

    candidate.refresh_from_db()
    assert result.recovery_needed is True
    assert candidate.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_locked_reconciliation_never_scans_queue_args(tenant, tenant_membership, refresh_job):
    # procrastinate_jobs.args has no index for a schema_id lookup; scanning it while
    # holding the tenant lock would serialize every refresh behind a table scan.
    legacy = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_lock_order", state=SchemaState.PROVISIONING
    )
    refresh_job(
        {"schema_id": str(legacy.id), "membership_id": str(tenant_membership.id)},
        status="failed",
    )
    legacy_jobs = find_legacy_refresh_jobs(tenant)

    with CaptureQueriesContext(connection) as queries:
        result = _reconcile(tenant, legacy_jobs)

    assert result.settled_schema_ids == (legacy.id,)
    assert not [
        q["sql"]
        for q in queries.captured_queries
        if "procrastinate_jobs" in q["sql"] and "'schema_id'" in q["sql"]
    ]


@pytest.fixture
def manage_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def queued_schema_drops():
    # procrastinate_jobs is unmanaged, so a real defer would outlive the test.
    with patch("apps.workspaces.tasks.drop_failed_refresh_schema.defer") as drop:
        yield drop


def _run_refresh(job_id, args):
    return async_to_sync(refresh_tenant_schema.func)(
        context=SimpleNamespace(job=SimpleNamespace(id=job_id)), **args
    )


def _post_refresh(client, workspace, *, job_id=987650):
    with patch("apps.workspaces.api.views.refresh_tenant_schema.defer") as defer:
        defer.return_value = MagicMock(id=job_id)
        response = client.post(f"/api/workspaces/{workspace.id}/refresh/")
    return response, defer


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("state", [SchemaState.ACTIVE, SchemaState.MATERIALIZING])
def test_duplicate_delivery_never_runs_physical_work(
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
        result = _run_refresh(job_id, args)

    candidate.refresh_from_db()
    assert result == {"status": "ignored"}
    assert candidate.state == state
    create.assert_not_called()
    pipeline.assert_not_called()
    teardown.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_mismatched_job_reports_a_request_mismatch_not_a_role(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, _job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    stray_job_id = refresh_job(args)

    with patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create:
        result = _run_refresh(stray_job_id, args)

    candidate.refresh_from_db()
    assert result["status"] == "rejected"
    assert result["error_code"] == ErrorCode.REFRESH_REQUEST_MISMATCH
    assert "role" not in result["error"].lower()
    assert candidate.state == SchemaState.PROVISIONING
    create.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("lose", "error_code", "phrase"),
    [
        ("role", ErrorCode.WORKSPACE_ROLE_INSUFFICIENT, "role required"),
        ("membership", ErrorCode.WORKSPACE_TENANT_UNREACHABLE, "no longer has access"),
        ("workspace_link", ErrorCode.WORKSPACE_TENANT_UNREACHABLE, "no longer part of"),
    ],
)
def test_denied_refresh_reports_which_authority_was_lost(
    workspace, tenant, tenant_membership, refresh_job, lose, error_code, phrase
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    if lose == "role":
        WorkspaceMembership.objects.filter(workspace=workspace, user=tenant_membership.user).update(
            role=WorkspaceRole.READ
        )
    elif lose == "membership":
        TenantMembership.objects.filter(id=tenant_membership.id).delete()
    else:
        WorkspaceTenant.objects.filter(workspace=workspace, tenant=tenant).delete()

    with patch("apps.workspaces.tasks.SchemaManager.create_physical_schema") as create:
        result = _run_refresh(job_id, args)

    candidate.refresh_from_db()
    assert result["status"] == "denied"
    assert result["error_code"] == error_code
    assert phrase in result["error"]
    assert result["retry_required"] is True
    assert candidate.state == SchemaState.FAILED
    create.assert_not_called()


def _stub_registry(tenant):
    pipeline = MagicMock(provider=tenant.provider)
    # ``name`` is a Mock constructor argument, so it must be set afterwards.
    pipeline.name = "refresh_pipeline"
    registry = MagicMock()
    registry.list.return_value = [pipeline]
    registry.get.return_value = pipeline
    return registry


@pytest.mark.django_db(transaction=True)
def test_pipeline_failure_cleanup_cannot_drop_candidate_that_became_active(
    workspace, tenant, tenant_membership, refresh_job
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)

    def activate_then_fail(*_args, **_kwargs):
        TenantSchema.objects.filter(id=candidate.id).update(state=SchemaState.ACTIVE)
        raise RuntimeError("late pipeline failure")

    with (
        patch("apps.workspaces.services.schema_manager.get_managed_db_connection"),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            return_value={"type": "api_key", "value": "token"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_stub_registry(tenant)),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=activate_then_fail),
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
    ):
        result = _run_refresh(job_id, args)

    candidate.refresh_from_db()
    assert result["error"] == "Materialization failed"
    assert candidate.state == SchemaState.ACTIVE
    teardown.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("taken_to", "dropped"), [(SchemaState.FAILED, True), (SchemaState.ACTIVE, False)]
)
def test_lost_activation_drops_the_loaded_schema_only_if_it_was_failed(
    workspace, tenant, tenant_membership, refresh_job, taken_to, dropped
):
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)

    def load_then_lose_ownership(*_args, **_kwargs):
        TenantSchema.objects.filter(id=candidate.id).update(state=taken_to)

    with (
        patch("apps.workspaces.services.schema_manager.get_managed_db_connection"),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            return_value={"type": "api_key", "value": "token"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_stub_registry(tenant)),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=load_then_lose_ownership),
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
    ):
        result = _run_refresh(job_id, args)

    candidate.refresh_from_db()
    assert result == {"status": "ignored"}
    assert candidate.state == taken_to
    assert [call.args[0].id for call in teardown.call_args_list] == (
        [candidate.id] if dropped else []
    )


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
def test_settled_candidate_is_retryable_and_its_schema_drop_queued(
    manage_client, workspace, tenant, tenant_membership, refresh_job, queued_schema_drops
):
    # The worker may have died after CREATE SCHEMA or mid-load; nothing else ever
    # sweeps FAILED rows, so settling one must also queue its physical drop.
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _set_job(job_id, status="failed")

    response, _defer = _post_refresh(manage_client, workspace, job_id=987657)

    candidate.refresh_from_db()
    assert response.status_code == 202
    assert candidate.state == SchemaState.FAILED
    assert TenantSchema.objects.get(id=response.data["schema_id"]).refresh_job_id == 987657
    queued_schema_drops.assert_called_once_with(schema_id=str(candidate.id))


@pytest.mark.django_db(transaction=True)
def test_candidate_whose_job_was_pruned_does_not_lock_out_refresh(
    manage_client, workspace, tenant, tenant_membership, refresh_job, queued_schema_drops
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _delete_job(job_id)
    _age_past_retention(candidate)

    response, defer = _post_refresh(manage_client, workspace)

    candidate.refresh_from_db()
    assert response.status_code == 202
    assert candidate.state == SchemaState.FAILED
    defer.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_in_flight_candidate_answers_already_in_progress(
    manage_client, workspace, tenant, tenant_membership, refresh_job, queued_schema_drops
):
    _bound_candidate(tenant, workspace, tenant_membership, refresh_job)

    response, defer = _post_refresh(manage_client, workspace)

    assert response.status_code == 409
    assert response.data == {"error": "A refresh is already in progress."}
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_initial_provision_row_answers_already_in_progress(
    manage_client, workspace, tenant, tenant_membership, queued_schema_drops
):
    TenantSchema.objects.create(
        tenant=tenant,
        schema_name=tenant_schema_name(tenant.provider, tenant.external_id),
        state=SchemaState.PROVISIONING,
    )

    response, defer = _post_refresh(manage_client, workspace)

    assert response.status_code == 409
    assert response.data == {"error": "A refresh is already in progress."}
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_unverifiable_candidate_requires_operator_recovery(
    manage_client, workspace, tenant, tenant_membership, refresh_job, queued_schema_drops
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _set_job(job_id, args={"schema_id": str(candidate.id)})

    response, defer = _post_refresh(manage_client, workspace)

    candidate.refresh_from_db()
    assert response.status_code == 409
    assert response.data["code"] == ErrorCode.REFRESH_RECOVERY_REQUIRED
    assert "operator" in response.data["error"].lower()
    assert candidate.state == SchemaState.PROVISIONING
    defer.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_legacy_job_search_runs_before_the_tenant_lock(
    manage_client, workspace, tenant, tenant_membership, refresh_job, queued_schema_drops
):
    legacy = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_lock_order", state=SchemaState.PROVISIONING
    )
    refresh_job(
        {"schema_id": str(legacy.id), "membership_id": str(tenant_membership.id)},
        status="failed",
    )

    with CaptureQueriesContext(connection) as queries:
        response, _defer = _post_refresh(manage_client, workspace)

    assert response.status_code == 202
    sql = [query["sql"] for query in queries.captured_queries]
    searches = [i for i, q in enumerate(sql) if "procrastinate_jobs" in q and "'schema_id'" in q]
    tenant_lock = next(
        (i for i, q in enumerate(sql) if 'FROM "users_tenant"' in q and "FOR UPDATE" in q),
        None,
    )
    assert tenant_lock is not None, "refresh view no longer takes the tenant row lock"
    assert searches
    assert max(searches) < tenant_lock
    assert all("FOR UPDATE" not in sql[i] for i in searches)


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
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM procrastinate_jobs WHERE task_name = %s AND args->>'workspace_id' = %s",
                [REFRESH_TASK_NAME, str(workspace_id)],
            )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("heartbeat_age", "settled"),
    [(None, True), (STALLED_AFTER + timedelta(minutes=1), True), (timedelta(0), False)],
    ids=["worker-gone", "heartbeat-stale", "heartbeat-fresh"],
)
@pytest.mark.parametrize("claimed", [False, True])
def test_bound_candidate_whose_worker_died_is_settled_and_reported(
    workspace,
    tenant,
    tenant_membership,
    refresh_job,
    queue_worker,
    caplog,
    heartbeat_age,
    settled,
    claimed,
):
    # SIGKILL/OOM leaves the job "doing" forever; without this the tenant could
    # never refresh again and nothing would say why.
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    if claimed:
        assert claim_refresh_candidate(job_id=job_id, **args).status == "claimed"
    worker_id = None if heartbeat_age is None else queue_worker(heartbeat_age=heartbeat_age)
    _run_on_worker(job_id, worker_id)

    with caplog.at_level(logging.INFO, logger="apps.workspaces.services.refresh_requests"):
        result = _reconcile(tenant)

    candidate.refresh_from_db()
    assert result.recovery_needed is False
    if settled:
        assert result.settled_schema_ids == (candidate.id,)
        assert candidate.state == SchemaState.FAILED
        assert [r.levelno for r in caplog.records if str(candidate.id) in r.getMessage()] == [
            logging.ERROR
        ]
    else:
        assert result.settled_schema_ids == ()
        assert candidate.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_legacy_candidate_whose_worker_died_is_settled(
    tenant, tenant_membership, refresh_job, queue_worker
):
    legacy = TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_dead_worker", state=SchemaState.PROVISIONING
    )
    job_id = refresh_job({"schema_id": str(legacy.id), "membership_id": str(tenant_membership.id)})
    _run_on_worker(job_id, queue_worker(heartbeat_age=STALLED_AFTER + timedelta(minutes=1)))

    result = _reconcile(tenant)

    legacy.refresh_from_db()
    assert result.settled_schema_ids == (legacy.id,)
    assert legacy.state == SchemaState.FAILED


@pytest.mark.django_db(transaction=True)
def test_operator_recovery_names_the_candidate_and_reason(
    workspace, tenant, tenant_membership, refresh_job, caplog
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _set_job(job_id, args={"schema_id": str(candidate.id)})

    with caplog.at_level(logging.WARNING, logger="apps.workspaces.services.refresh_requests"):
        assert _reconcile(tenant).recovery_needed is True

    [record] = [r for r in caplog.records if str(candidate.id) in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "does not match the recorded request" in record.getMessage()


@pytest.mark.django_db(transaction=True)
def test_unbound_candidate_created_after_the_scan_stays_in_flight(tenant):
    legacy_jobs = find_legacy_refresh_jobs(tenant)
    newer = TenantSchema.objects.create(
        tenant=tenant, schema_name="unbound_after_scan", state=SchemaState.PROVISIONING
    )

    result = _reconcile(tenant, legacy_jobs)

    newer.refresh_from_db()
    assert (result.recovery_needed, result.settled_schema_ids) == (False, ())
    assert newer.state == SchemaState.PROVISIONING


@pytest.mark.django_db(transaction=True)
def test_one_queue_scan_serves_every_unbound_candidate(tenant, tenant_membership, refresh_job):
    first, second = (
        TenantSchema.objects.create(
            tenant=tenant, schema_name=f"legacy_batch_{i}", state=SchemaState.PROVISIONING
        )
        for i in range(2)
    )
    for candidate in (first, second, second):
        refresh_job(
            {"schema_id": str(candidate.id), "membership_id": str(tenant_membership.id)},
            status="failed",
        )

    with CaptureQueriesContext(connection) as queries:
        legacy_jobs = find_legacy_refresh_jobs(tenant)

    scans = [q for q in queries.captured_queries if "procrastinate_jobs" in q["sql"]]
    assert len(scans) == 1
    assert len(legacy_jobs.job_ids[first.id]) == 1
    assert len(legacy_jobs.job_ids[second.id]) == 2


@pytest.mark.django_db(transaction=True)
def test_periodic_sweep_settles_dead_refresh_without_a_retry(
    manage_client, workspace, tenant, tenant_membership, refresh_job, queue_worker
):
    candidate, _args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)
    _run_on_worker(job_id, None)

    with patch("apps.workspaces.tasks.drop_failed_refresh_schema.defer") as drop:
        result = async_to_sync(reconcile_refresh_candidates.func)()

    candidate.refresh_from_db()
    assert result == {"settled": 1, "recovery_needed": 0}
    assert candidate.state == SchemaState.FAILED
    drop.assert_called_once_with(schema_id=str(candidate.id))
    status_response = manage_client.get(f"/api/workspaces/{workspace.id}/refresh/status/")
    assert status_response.data["state"] == SchemaState.FAILED
    assert status_response.data["error"]


@pytest.mark.django_db(transaction=True)
def test_real_enqueue_is_claimed_and_published_by_the_worker(
    manage_client, workspace, tenant, tenant_membership
):
    # No mocked defer: the view's queued arguments must be exactly what the task
    # accepts and what the claim compares against.
    response = manage_client.post(f"/api/workspaces/{workspace.id}/refresh/")
    assert response.status_code == 202
    candidate = TenantSchema.objects.get(id=response.data["schema_id"])
    try:
        job = ProcrastinateJob.objects.get(id=candidate.refresh_job_id)
        assert job.task_name == REFRESH_TASK_NAME
        assert job.args == refresh_task_args(candidate)
        assert all(isinstance(value, str) for value in job.args.values())
        _set_job(job.id, status="doing")

        with (
            patch("apps.workspaces.services.schema_manager.get_managed_db_connection"),
            patch(
                "apps.workspaces.tasks.aresolve_credential",
                return_value={"type": "api_key", "value": "token"},
            ),
            patch("apps.workspaces.tasks.get_registry", return_value=_stub_registry(tenant)),
            patch("apps.workspaces.tasks.run_pipeline") as pipeline,
            patch("apps.workspaces.tasks._rebuild_dependent_view_schemas"),
            patch("apps.workspaces.tasks._rebuild_single_tenant_semantic_models"),
        ):
            result = _run_refresh(job.id, job.args)

        candidate.refresh_from_db()
        assert result["status"] == "active", result
        assert pipeline.call_args.kwargs["target_schema"].id == candidate.id
        assert candidate.state == SchemaState.ACTIVE
        assert candidate.refresh_claimed_at is not None
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM procrastinate_jobs WHERE task_name = %s AND args->>'workspace_id' = %s",
                [REFRESH_TASK_NAME, str(workspace.id)],
            )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", ["create_schema", "pipeline"])
def test_failure_cleanup_drops_schema_the_reconciler_already_failed(
    workspace, tenant, tenant_membership, refresh_job, failure
):
    # A false stall lets the reconciler settle the candidate, and its queued drop may
    # run before this still-live job's last write recreates the schema. The job must
    # then drop it itself, as the activation path already does.
    candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)

    def settle_then_fail(*_args, **_kwargs):
        TenantSchema.objects.filter(id=candidate.id).update(state=SchemaState.FAILED)
        raise RuntimeError("late failure")

    with (
        patch("apps.workspaces.services.schema_manager.get_managed_db_connection"),
        patch(
            "apps.workspaces.tasks.SchemaManager.create_physical_schema",
            side_effect=settle_then_fail if failure == "create_schema" else None,
        ),
        patch(
            "apps.workspaces.tasks.aresolve_credential",
            return_value={"type": "api_key", "value": "token"},
        ),
        patch("apps.workspaces.tasks.get_registry", return_value=_stub_registry(tenant)),
        patch("apps.workspaces.tasks.run_pipeline", side_effect=settle_then_fail),
        patch("apps.workspaces.tasks.SchemaManager.teardown") as teardown,
    ):
        _run_refresh(job_id, args)

    candidate.refresh_from_db()
    assert candidate.state == SchemaState.FAILED
    assert [call.args[0].id for call in teardown.call_args_list] == [candidate.id]


@pytest.mark.django_db(transaction=True)
def test_claim_without_schema_is_not_reported_as_a_role_failure(
    workspace, tenant, tenant_membership, refresh_job, caplog
):
    _candidate, args, job_id = _bound_candidate(tenant, workspace, tenant_membership, refresh_job)

    with (
        patch(
            "apps.workspaces.tasks.claim_refresh_candidate",
            return_value=RefreshClaim(status="claimed"),
        ),
        caplog.at_level(logging.ERROR, logger="apps.workspaces.tasks"),
    ):
        result = _run_refresh(job_id, args)

    assert result["error_code"] != ErrorCode.WORKSPACE_ROLE_INSUFFICIENT
    assert "role" not in result["error"].lower()
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.django_db(transaction=True)
def test_legacy_materializing_row_does_not_block_refresh(
    manage_client, workspace, tenant, tenant_membership
):
    # Nothing persists MATERIALIZING and no reconciler clears it, so a leftover row
    # in that state must not answer "in progress" forever.
    TenantSchema.objects.create(
        tenant=tenant, schema_name="legacy_materializing", state=SchemaState.MATERIALIZING
    )

    response, defer = _post_refresh(manage_client, workspace)

    assert response.status_code == 202
    defer.assert_called_once()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("inserted_before_scan", "recovery_needed"),
    [(timedelta(seconds=30), False), (UNSCANNED_GRACE + timedelta(minutes=1), True)],
    ids=["commit-lagging-insert", "long-unseen"],
)
def test_unbound_candidate_inserted_before_scan_but_committed_after_stays_in_flight(
    tenant, inserted_before_scan, recovery_needed
):
    # created_at is stamped at INSERT; an old-code refresh can insert before the scan
    # and commit after it, so a fresh candidate the scan could not see stays in flight.
    legacy_jobs = find_legacy_refresh_jobs(tenant)
    late = TenantSchema.objects.create(
        tenant=tenant, schema_name="unbound_commit_lag", state=SchemaState.PROVISIONING
    )
    TenantSchema.objects.filter(id=late.id).update(
        created_at=legacy_jobs.scanned_at - inserted_before_scan
    )

    result = _reconcile(tenant, legacy_jobs)

    late.refresh_from_db()
    assert result.recovery_needed is recovery_needed
    assert late.state == SchemaState.PROVISIONING
