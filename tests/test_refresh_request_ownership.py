"""Refresh requests are bound to one candidate, actor, workspace, and queue job."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

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
    activate_claimed_refresh_candidate,
    claim_refresh_candidate,
    fail_claimed_refresh_candidate,
    find_legacy_refresh_jobs,
    reconcile_legacy_refresh_candidates,
)

JOB_RETENTION = timedelta(hours=24 * 7)


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


def _reconcile(tenant):
    return reconcile_legacy_refresh_candidates(
        tenant,
        find_legacy_refresh_jobs(tenant),
        pruned_before=timezone.now() - JOB_RETENTION,
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
@pytest.mark.parametrize("job_status", ["cancelled", "failed", "succeeded"])
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
        result = reconcile_legacy_refresh_candidates(
            tenant, legacy_jobs, pruned_before=timezone.now() - JOB_RETENTION
        )

    assert result.settled_schema_ids == (legacy.id,)
    assert not [
        q["sql"]
        for q in queries.captured_queries
        if "procrastinate_jobs" in q["sql"] and "'schema_id'" in q["sql"]
    ]
