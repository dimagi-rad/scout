"""The periodic sweep reclaims workspace-load candidates no writer will finish.

Real control PostgreSQL and real tenant advisory locks; only the drop task's
enqueue is stubbed.
"""

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone
from procrastinate.exceptions import AlreadyEnqueued

from apps.workspaces import tasks as workspaces_tasks
from apps.workspaces.models import (
    MaterializationRun,
    SchemaState,
    TenantLoadGeneration,
    TenantSchema,
)
from apps.workspaces.services.data_operation import (
    LockOrderError,
    tenant_data_lock,
    tenant_data_lock_if_free,
)
from apps.workspaces.services.load_candidates import candidate_last_attempt_at

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _candidate(tenant, workspace, *, state, generation, age=timedelta(0)):
    schema = await TenantSchema.objects.acreate(
        tenant=tenant,
        schema_name=f"cand_{uuid.uuid4().hex[:8]}",
        state=state,
        load_workspace_id=workspace.id,
        load_job_id=1,
        load_generation=generation,
        load_config_fingerprint="cfg",
    )
    if age:
        await TenantSchema.objects.filter(id=schema.id).aupdate(created_at=timezone.now() - age)
    return schema


async def _ledger(tenant, *, requested, published, loading=0):
    await TenantLoadGeneration.objects.aupdate_or_create(
        tenant=tenant,
        defaults={
            "requested_generation": requested,
            "published_generation": published,
            "loading_generation": loading,
        },
    )


async def _sweep():
    with patch.object(workspaces_tasks.drop_abandoned_candidate, "configure") as configure:
        configure.return_value.defer_async = AsyncMock(return_value=1)
        counts = await workspaces_tasks.sweep_workspace_load_candidates()
    queued = {
        call.kwargs["schema_id"] for call in configure.return_value.defer_async.await_args_list
    }
    return counts, queued


async def test_a_dead_writers_candidate_is_settled_and_kept_for_its_retry(tenant, workspace):
    await _ledger(tenant, requested=2, published=1, loading=2)
    orphan = await _candidate(tenant, workspace, state=SchemaState.PROVISIONING, generation=2)

    counts, queued = await _sweep()

    await orphan.arefresh_from_db()
    assert orphan.state == SchemaState.FAILED
    assert counts["settled"] == 1
    assert queued == set()
    # The dead writer's marker is cleared, so the next request joins and resumes.
    ledger = await TenantLoadGeneration.objects.aget(tenant=tenant)
    assert ledger.loading_generation == 0


async def test_candidates_no_pending_load_will_resume_are_queued_for_dropping(tenant, workspace):
    await _ledger(tenant, requested=3, published=2)
    superseded = await _candidate(tenant, workspace, state=SchemaState.FAILED, generation=2)
    stale = await _candidate(
        tenant, workspace, state=SchemaState.FAILED, generation=3, age=timedelta(days=2)
    )
    resumable = await _candidate(tenant, workspace, state=SchemaState.FAILED, generation=3)

    counts, queued = await _sweep()

    assert queued == {str(superseded.id), str(stale.id)}
    assert str(resumable.id) not in queued
    assert counts["drops_queued"] == 2


async def test_with_nothing_pending_every_failed_candidate_is_abandoned(tenant, workspace):
    await _ledger(tenant, requested=2, published=2)
    failed = await _candidate(tenant, workspace, state=SchemaState.FAILED, generation=2)

    _counts, queued = await _sweep()

    assert queued == {str(failed.id)}


async def test_a_tenant_with_a_live_writer_is_skipped(tenant, workspace):
    await _ledger(tenant, requested=2, published=1, loading=2)
    in_flight = await _candidate(tenant, workspace, state=SchemaState.PROVISIONING, generation=2)

    async with tenant_data_lock([tenant.id]):
        # The sweep runs as its own task, so it sees another session's lock.
        counts, queued = await asyncio.create_task(_sweep())

    await in_flight.arefresh_from_db()
    assert in_flight.state == SchemaState.PROVISIONING
    assert counts["skipped_busy"] == 1
    assert queued == set()


async def test_a_drop_already_queued_is_not_queued_again(tenant, workspace):
    await _ledger(tenant, requested=2, published=2)
    await _candidate(tenant, workspace, state=SchemaState.FAILED, generation=2)

    with patch.object(workspaces_tasks.drop_abandoned_candidate, "configure") as configure:
        configure.return_value.defer_async = AsyncMock(side_effect=AlreadyEnqueued("queued"))
        counts = await workspaces_tasks.sweep_workspace_load_candidates()

    assert counts["drops_queued"] == 0
    assert configure.call_args.kwargs["queueing_lock"].startswith("drop_abandoned_candidate:")


async def test_the_non_waiting_lock_reuses_a_held_tenant_and_refuses_expansion():
    held, other = uuid.uuid4(), uuid.uuid4()
    async with tenant_data_lock([held]):
        async with tenant_data_lock_if_free(held) as locked:
            assert locked is True
        with pytest.raises(LockOrderError):
            async with tenant_data_lock_if_free(other):
                pass


async def _attempt(schema):
    return await MaterializationRun.objects.acreate(
        tenant_schema=schema,
        pipeline="commcare_sync",
        state=MaterializationRun.RunState.FAILED,
        result={"sources": {}},
    )


async def test_a_pending_candidate_attempted_recently_is_kept_despite_its_age(tenant, workspace):
    """A resume reuses the row, so age is measured from the last attempt, not creation."""
    await _ledger(tenant, requested=3, published=2)
    retried = await _candidate(
        tenant, workspace, state=SchemaState.FAILED, generation=3, age=timedelta(days=2)
    )
    await _attempt(retried)

    _counts, queued = await _sweep()

    assert queued == set()


async def test_a_drop_skips_a_candidate_resumed_after_it_was_queued(tenant, workspace):
    """Queued as stale, then resumed and failed again: it is the pending
    generation's resume point again and must survive the queued drop."""
    candidate = await _candidate(
        tenant, workspace, state=SchemaState.FAILED, generation=3, age=timedelta(days=2)
    )
    judged_on = await sync_to_async(candidate_last_attempt_at)(candidate.id)
    await _attempt(candidate)

    with patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None) as teardown:
        await workspaces_tasks.drop_abandoned_candidate(
            schema_id=str(candidate.id), last_attempt_at=judged_on.isoformat()
        )

    teardown.assert_not_called()
    await candidate.arefresh_from_db()
    assert candidate.state == SchemaState.FAILED


async def test_a_drop_proceeds_when_nothing_attempted_the_candidate_since(tenant, workspace):
    candidate = await _candidate(tenant, workspace, state=SchemaState.FAILED, generation=1)
    judged_on = await sync_to_async(candidate_last_attempt_at)(candidate.id)

    with patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None) as teardown:
        await workspaces_tasks.drop_abandoned_candidate(
            schema_id=str(candidate.id), last_attempt_at=judged_on.isoformat()
        )

    teardown.assert_called_once()
    await candidate.arefresh_from_db()
    assert candidate.state == SchemaState.EXPIRED


async def test_a_drop_never_waits_on_a_loading_tenant(tenant, workspace):
    candidate = await _candidate(tenant, workspace, state=SchemaState.FAILED, generation=1)

    async def drop_while_loading():
        with (
            patch("apps.workspaces.tasks.SchemaManager.teardown", return_value=None) as teardown,
            patch.object(workspaces_tasks.drop_abandoned_candidate, "configure") as retry,
        ):
            retry.return_value.defer_async = AsyncMock(return_value=1)
            await workspaces_tasks.drop_abandoned_candidate(schema_id=str(candidate.id))
        return teardown, retry

    async with tenant_data_lock([tenant.id]):
        teardown, retry = await asyncio.wait_for(asyncio.create_task(drop_while_loading()), 10)

    teardown.assert_not_called()
    retry.assert_called_once()
    await candidate.arefresh_from_db()
    assert candidate.state == SchemaState.FAILED
