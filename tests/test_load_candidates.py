"""Candidate lifecycle for shared-tenant loads: open, resume, promote, fail, settle."""

import uuid

import pytest
from django.utils import timezone

from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
)
from apps.workspaces.services.data_operation import LockOrderError, sync_tenant_data_lock
from apps.workspaces.services.load_candidates import (
    abandoned_workspace_candidates,
    fail_workspace_candidate,
    load_owner_token,
    open_workspace_candidate,
    promote_candidate_schema,
    settle_orphaned_workspace_candidates,
)
from apps.workspaces.services.load_generations import (
    INTENT_FULL_REFRESH,
    begin_load_generation,
    capture_load_intent,
    reusable_generation,
)
from apps.workspaces.services.refresh_requests import find_legacy_refresh_jobs

pytestmark = pytest.mark.django_db(transaction=True)

CONFIG = "raw-config-a"
FINGERPRINT = "current-fingerprint"
JOB = 11


def _open(tenant, workspace, *, generation, job_id=JOB, config=CONFIG):
    with sync_tenant_data_lock([tenant.id]):
        return open_workspace_candidate(
            tenant,
            workspace_id=workspace.id,
            job_id=job_id,
            generation=generation,
            config_fingerprint=config,
        )


def _abandoned(tenant, keep_id):
    with sync_tenant_data_lock([tenant.id]):
        return abandoned_workspace_candidates(tenant.id, keep_id=keep_id)


def _completed_run(schema, *, job_id=JOB, result=None, state=None):
    return MaterializationRun.objects.create(
        tenant_schema=schema,
        pipeline="commcare_sync",
        procrastinate_job_id=job_id,
        state=state or MaterializationRun.RunState.COMPLETED,
        result=result if result is not None else {"sources": {}, "load_fingerprint": FINGERPRINT},
    )


def _promote(candidate, workspace, generation, run, *, job_id=JOB, fingerprint=FINGERPRINT):
    return promote_candidate_schema(
        candidate.id,
        accessed_at=timezone.now(),
        workspace_id=workspace.id,
        workspace_job_id=job_id,
        loading_generation=generation,
        run_id=run.id if run is not None else None,
        fingerprint=fingerprint,
    )


def test_a_fresh_candidate_records_its_owner_generation_and_config(tenant, workspace):
    generation = begin_load_generation(tenant.id)
    opened = _open(tenant, workspace, generation=generation, job_id=7)

    assert not opened.resumed
    schema = opened.schema
    assert schema.state == SchemaState.PROVISIONING
    assert (schema.load_workspace_id, schema.load_job_id) == (workspace.id, 7)
    assert (schema.load_generation, schema.load_config_fingerprint) == (generation, CONFIG)


def test_a_failed_candidate_is_resumed_by_the_same_generation_and_config(tenant, workspace):
    generation = begin_load_generation(tenant.id)
    first = _open(tenant, workspace, generation=generation, job_id=1).schema
    assert fail_workspace_candidate(first.id, workspace.id, 1) is not None

    again = _open(tenant, workspace, generation=generation, job_id=2)

    assert again.resumed
    assert again.schema.id == first.id
    assert again.schema.state == SchemaState.PROVISIONING
    assert again.schema.load_job_id == 2
    assert _abandoned(tenant, again.schema.id) == []


@pytest.mark.parametrize("change", ["config", "generation"])
def test_a_mismatched_load_starts_fresh_and_abandons_the_old_candidate(tenant, workspace, change):
    generation = begin_load_generation(tenant.id)
    first = _open(tenant, workspace, generation=generation, job_id=1).schema
    fail_workspace_candidate(first.id, workspace.id, 1)
    if change == "generation":
        TenantLoadGeneration.objects.filter(tenant=tenant).update(
            requested_generation=generation + 1
        )

    again = _open(
        tenant,
        workspace,
        generation=generation + 1 if change == "generation" else generation,
        config="raw-config-b" if change == "config" else CONFIG,
        job_id=2,
    )

    assert not again.resumed
    assert again.schema.id != first.id
    assert [c.id for c in _abandoned(tenant, again.schema.id)] == [first.id]


@pytest.mark.parametrize(
    "invalid",
    [
        "cancelled",
        "partial",
        "failed",
        "missing_run",
        "transform_error",
        "wrong_schema",
        "wrong_job",
        "missing_receipt",
        "wrong_receipt",
    ],
)
def test_promotion_rejects_invalid_run_without_retiring_last_good(tenant, workspace, invalid):
    active = TenantSchema.objects.create(
        tenant=tenant, schema_name="promotion_last_good", state=SchemaState.ACTIVE
    )
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation).schema
    state = (
        invalid
        if invalid in ("cancelled", "partial", "failed")
        else MaterializationRun.RunState.COMPLETED
    )
    result = {"sources": {}, "load_fingerprint": FINGERPRINT}
    if invalid == "transform_error":
        result["transforms"] = {"error": "dbt failed"}
    elif invalid == "missing_receipt":
        result.pop("load_fingerprint")
    elif invalid == "wrong_receipt":
        result["load_fingerprint"] = "different-execution"
    run = _completed_run(
        active if invalid == "wrong_schema" else candidate,
        job_id=999 if invalid == "wrong_job" else JOB,
        result=result,
        state=state,
    )

    outcome = _promote(candidate, workspace, generation, None if invalid == "missing_run" else run)

    assert not outcome.promoted
    candidate.refresh_from_db()
    active.refresh_from_db()
    assert candidate.state == SchemaState.PROVISIONING
    assert active.state == SchemaState.ACTIVE


@pytest.mark.parametrize("tests_failed", [False, True])
def test_promotion_swaps_active_atomically_and_publishes_the_generation(
    tenant, workspace, tests_failed
):
    old = TenantSchema.objects.create(tenant=tenant, schema_name="old", state=SchemaState.ACTIVE)
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation).schema
    transforms = {"status": "tests_failed", "error": "quality assertion"} if tests_failed else {}
    run = _completed_run(
        candidate, result={"sources": {}, "load_fingerprint": FINGERPRINT, "transforms": transforms}
    )

    outcome = _promote(candidate, workspace, generation, run)

    assert outcome.promoted
    assert outcome.retired_schema_ids == (old.id,)
    states = dict(TenantSchema.objects.values_list("id", "state"))
    assert states[candidate.id] == SchemaState.ACTIVE
    assert states[old.id] == SchemaState.TEARDOWN
    ledger = TenantLoadGeneration.objects.get(tenant=tenant)
    assert ledger.published_generation == generation
    assert (ledger.published_run_id, ledger.published_schema_id) == (run.id, candidate.id)
    assert ledger.published_fingerprint == FINGERPRINT


@pytest.mark.parametrize("mismatch", ["generation", "candidate_job", "other_workspace"])
def test_promotion_rejects_superseded_generation_or_foreign_owner(tenant, workspace, mismatch):
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation, job_id=42).schema
    run = _completed_run(candidate, job_id=42)

    outcome = promote_candidate_schema(
        candidate.id,
        accessed_at=timezone.now(),
        workspace_id=uuid.uuid4() if mismatch == "other_workspace" else workspace.id,
        workspace_job_id=43 if mismatch == "candidate_job" else 42,
        loading_generation=generation + 1 if mismatch == "generation" else generation,
        run_id=run.id,
        fingerprint=FINGERPRINT,
    )

    assert not outcome.promoted


def test_only_the_owning_load_can_fail_its_candidate(tenant, workspace):
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation, job_id=5).schema

    assert fail_workspace_candidate(candidate.id, workspace.id, 6) is None
    assert fail_workspace_candidate(candidate.id, workspace.id, 5).state == SchemaState.FAILED
    # Already FAILED: a second failure is a no-op, not a state change.
    assert fail_workspace_candidate(candidate.id, workspace.id, 5) is None


def test_orphans_settled_while_holding_t_become_resumable(tenant, workspace):
    generation = begin_load_generation(tenant.id)
    orphan = _open(tenant, workspace, generation=generation, job_id=1).schema
    refresh = TenantSchema.objects.create(
        tenant=tenant, schema_name="refresh_cand", state=SchemaState.PROVISIONING, refresh_job_id=9
    )

    with sync_tenant_data_lock([tenant.id]):
        settled = settle_orphaned_workspace_candidates(tenant.id)

    assert [s.id for s in settled] == [orphan.id]
    refresh.refresh_from_db()
    assert refresh.state == SchemaState.PROVISIONING
    assert _open(tenant, workspace, generation=generation, job_id=2).schema.id == orphan.id


def test_refresh_reconciliation_never_treats_a_workspace_candidate_as_legacy(tenant, workspace):
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation).schema

    assert candidate.id not in find_legacy_refresh_jobs(tenant).job_ids


def test_a_load_still_publishes_after_a_refresh_asked_for_the_next_generation(tenant, workspace):
    """A refresh clicked mid-load moves requested_generation on; the running load
    still publishes its own generation, and the new request stays pending."""
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation).schema
    capture_load_intent([tenant.id], INTENT_FULL_REFRESH)
    run = _completed_run(candidate)

    assert _promote(candidate, workspace, generation, run).promoted
    ledger = TenantLoadGeneration.objects.get(tenant=tenant)
    assert (ledger.published_generation, ledger.requested_generation) == (
        generation,
        generation + 1,
    )


def test_a_load_without_a_job_id_cannot_prove_ownership(tenant, workspace):
    """None == None must not stand in for ownership: it would accept any job-less
    run on the schema, including another attempt's."""
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation, job_id=None).schema
    run = _completed_run(candidate, job_id=None)

    assert not _promote(candidate, workspace, generation, run, job_id=None).promoted
    candidate.refresh_from_db()
    assert candidate.state == SchemaState.PROVISIONING


def test_a_resumed_then_published_generation_stays_reusable(tenant, workspace):
    generation = begin_load_generation(tenant.id)
    first = _open(tenant, workspace, generation=generation, job_id=1).schema
    failed_run = _completed_run(first, job_id=1, state=MaterializationRun.RunState.FAILED)
    fail_workspace_candidate(first.id, workspace.id, 1)
    resumed = _open(tenant, workspace, generation=generation, job_id=2)
    assert resumed.resumed
    run = _completed_run(resumed.schema, job_id=2)

    assert _promote(resumed.schema, workspace, generation, run, job_id=2).promoted

    failed_run.refresh_from_db()
    assert failed_run.state == MaterializationRun.RunState.STALE
    evidence = reusable_generation(tenant.id, generation, FINGERPRINT)
    assert evidence is not None
    assert evidence.run.id == run.id


def test_listing_abandoned_candidates_requires_the_kept_one():
    with pytest.raises(TypeError):
        abandoned_workspace_candidates(uuid.uuid4())


def test_a_job_less_load_owns_its_candidate_through_a_per_attempt_token(tenant, workspace):
    first, second = load_owner_token(None), load_owner_token(None)
    assert first < 0
    assert second < 0
    assert first != second
    assert load_owner_token(42) == 42
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation, job_id=first).schema
    run = _completed_run(candidate, job_id=None)

    assert not _promote(candidate, workspace, generation, run, job_id=second).promoted
    assert _promote(candidate, workspace, generation, run, job_id=first).promoted


@pytest.mark.parametrize("operation", ["settle", "open", "abandoned"])
def test_candidate_lifecycle_changes_refuse_a_caller_without_t(tenant, workspace, operation):
    """The Tenant row lock is released at commit; only T spans a writer's load, so
    a caller without it could fail a live writer's candidate underneath it."""
    generation = begin_load_generation(tenant.id)
    live = _open(tenant, workspace, generation=generation).schema
    calls = {
        "settle": lambda: settle_orphaned_workspace_candidates(tenant.id),
        "open": lambda: open_workspace_candidate(
            tenant,
            workspace_id=workspace.id,
            job_id=JOB,
            generation=generation,
            config_fingerprint=CONFIG,
        ),
        "abandoned": lambda: abandoned_workspace_candidates(tenant.id, keep_id=live.id),
    }

    with pytest.raises(LockOrderError):
        calls[operation]()
    live.refresh_from_db()
    assert live.state == SchemaState.PROVISIONING


def test_holding_another_tenants_t_does_not_count(tenant, workspace):
    with sync_tenant_data_lock([uuid.uuid4()]), pytest.raises(LockOrderError):
        settle_orphaned_workspace_candidates(tenant.id)


def test_a_tests_failed_generation_is_published_and_reusable(tenant, workspace):
    """Promotion publishes a run whose data-quality tests failed; reuse must accept
    the same run, or that fingerprint would reload forever."""
    generation = begin_load_generation(tenant.id)
    candidate = _open(tenant, workspace, generation=generation).schema
    run = _completed_run(
        candidate,
        result={
            "sources": {},
            "load_fingerprint": FINGERPRINT,
            "transforms": {"status": "tests_failed", "error": "quality assertion"},
        },
    )

    assert _promote(candidate, workspace, generation, run).promoted
    assert reusable_generation(tenant.id, generation, FINGERPRINT) is not None
